# -*- coding: utf-8 -*-
"""ATAMŪRA Core — кокпит над продуктами (финблок / договоры / СБ / Кронос / Оракл).
ПРИНЦИП: кокпит ТОЛЬКО ЧИТАЕТ снимки продуктов и агрегирует — логику продуктов НЕ дублирует.

Локальный старт (Windows): просто `python server.py` → http://127.0.0.1:8090
Источники (env, все опциональны):
  DOGOVOR_LOGS   — папка логов генераций бота договоров (по умолч. рядом на Desktop)
  FINANCE_URL    — база финблока (напр. https://finance.atamura.group), + FINANCE_KEY (X-Service-Key)
  BITRIX_WEBHOOK — чтобы резолвить инициатора ID → Фамилия (иначе показываем ID)
"""
import os, re, json, glob, ssl, urllib.request, urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CORE_PORT", "8090"))
DOGOVOR_LOGS = os.environ.get("DOGOVOR_LOGS",
    os.path.join(os.path.dirname(BASE), "atamura-dogovor-bot", "logs", "generations"))
FINANCE_URL = os.environ.get("FINANCE_URL", "").rstrip("/")
FINANCE_KEY = os.environ.get("FINANCE_KEY", "")
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/")
_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE


# ---------- источник: БОТ ДОГОВОРОВ → косяки инициаторов ----------
def _flag_kind(flag):
    """Грубая категория косяка (для группировки)."""
    t = str(flag or "").lower()
    if "бин" in t: return "БИН не совпадает"
    if "сумм" in t: return "Сумма не указана"
    if "порядок оплаты" in t: return "Порядок оплаты не указан"
    if "директор" in t or "руководител" in t or "устав" in t: return "Директор/Устав (Adata≠вложение)"
    if "тип договор" in t or "вид договора" in t: return "Тип договора (подряд/услуга)"
    if "реквизит" in t: return "Реквизиты не те"
    if "срок" in t or "дата" in t: return "Сроки/даты"
    return re.sub(r"[\[\]🔴🟡🟢]", "", str(flag)).strip()[:40] or "прочее"


def dogovor_kosyaki():
    """Агрегат косяков по инициатору из логов генераций бота договоров.
    На карточку берём ТОЛЬКО последнюю генерацию (перегенерации не задваивают косяки)."""
    files = glob.glob(os.path.join(DOGOVOR_LOGS, "*", "*.json"))
    latest = {}
    for fp in sorted(files):                       # путь = дата-папка + имя → последняя в конце
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        cid = str(d.get("card_id") or os.path.basename(fp).split("_")[0])
        latest[cid] = d
    by_init = defaultdict(lambda: {"zayavok": 0, "kosyakov": 0, "kinds": defaultdict(int), "examples": []})
    total_z = total_k = 0
    for d in latest.values():
        cs = d.get("card_snapshot", {})
        init = str(cs.get("Ответственный") or cs.get("Отдел - инициатор") or "—")
        flags = [f for f in (d.get("flags") or []) if str(f).strip()]
        rec = by_init[init]
        rec["zayavok"] += 1; total_z += 1
        for f in flags:
            rec["kosyakov"] += 1; total_k += 1
            rec["kinds"][_flag_kind(f)] += 1
            if len(rec["examples"]) < 3:
                rec["examples"].append({"num": cs.get("Название", "")[:44], "flag": re.sub(r"[\[\]]", "", str(f))[:70]})
    names = _bx_names(list(by_init.keys()))
    rows = []
    for init, r in by_init.items():
        top = sorted(r["kinds"].items(), key=lambda x: -x[1])
        rows.append({"initiator_id": init, "initiator": names.get(init, "ID " + init),
                     "zayavok": r["zayavok"], "kosyakov": r["kosyakov"],
                     "top_kind": top[0][0] if top else "—",
                     "kinds": [{"kind": k, "n": n} for k, n in top], "examples": r["examples"]})
    rows.sort(key=lambda x: -x["kosyakov"])
    return {"rows": rows, "total_zayavok": total_z, "total_kosyakov": total_k,
            "source": DOGOVOR_LOGS, "files": len(files)}


_NAME_CACHE = {}
def _bx_names(ids):
    """Инициатор Bitrix ID → «Фамилия И.» через user.get (если задан BITRIX_WEBHOOK)."""
    out = {}
    if not BITRIX_WEBHOOK:
        return out
    for uid in ids:
        if not uid.isdigit():
            continue
        if uid in _NAME_CACHE:
            out[uid] = _NAME_CACHE[uid]; continue
        try:
            req = urllib.request.Request(f"{BITRIX_WEBHOOK}/user.get.json?ID={uid}")
            r = json.load(urllib.request.urlopen(req, context=_CTX, timeout=15))
            u = (r.get("result") or [{}])[0]
            nm = (u.get("LAST_NAME", "") + " " + (u.get("NAME", "")[:1] + "." if u.get("NAME") else "")).strip() or ("ID " + uid)
            _NAME_CACHE[uid] = nm; out[uid] = nm
        except Exception:
            pass
    return out


# ---------- источник: ФИНБЛОК → финсводка (снимок по HTTP) ----------
def finance_snapshot():
    if not FINANCE_URL:
        return {"connected": False, "hint": "Задай FINANCE_URL (+ FINANCE_KEY), чтобы подтянуть финсводку."}
    try:
        req = urllib.request.Request(FINANCE_URL + "/data.json",
                                     headers={"X-Service-Key": FINANCE_KEY} if FINANCE_KEY else {})
        d = json.load(urllib.request.urlopen(req, context=_CTX, timeout=25))
    except Exception as e:
        return {"connected": False, "error": str(e)[:160], "hint": "Финблок недоступен по FINANCE_URL."}
    series = d.get("series", []); counts = d.get("counts", {}); meta = d.get("meta", {})
    out_sum = sum(s.get("out", 0) for s in series); in_sum = sum(s.get("in", 0) for s in series)
    return {"connected": True, "otток": out_sum, "приток": in_sum, "saldo": in_sum - out_sum,
            "matched": counts.get("matched"), "reserve": counts.get("reserve"),
            "nz_orphan": counts.get("nz_orphan"), "ts": meta.get("ts"), "months": meta.get("months")}


# ---------- HTTP ----------
def _money(n):
    try:
        return ("{:,.0f}".format(n or 0)).replace(",", " ")
    except Exception:
        return str(n)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        try:
            if self.path == "/api/kosyaki":
                self._send(json.dumps(dogovor_kosyaki(), ensure_ascii=False), "application/json")
            elif self.path == "/api/finance":
                self._send(json.dumps(finance_snapshot(), ensure_ascii=False), "application/json")
            elif self.path == "/healthz":
                self._send("ok", "text/plain")
            else:
                self._send(PAGE)
        except Exception as e:
            self._send(f"<pre>err: {e}</pre>", code=500)


PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ATAMŪRA Core · Кокпит</title>
<style>
:root{--bg:#0b1420;--panel:#12222f;--panel2:#17293a;--ink:#e7eef5;--ink2:#a6b8ca;--muted:#75879a;
 --line:#22384c;--accent:#4d8bf0;--good:#46c46f;--warn:#e0a552;--crit:#f06a62}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.55 -apple-system,Segoe UI,Roboto,Arial}
.top{padding:18px 26px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
.glyph{display:grid;grid-template-columns:1fr 1fr;gap:3px}.glyph span{width:9px;height:9px;border-radius:50%;background:var(--accent)}
.top b{letter-spacing:.28em;font-size:14px}.top small{color:var(--muted);letter-spacing:.4em;font-size:9px;display:block}
.wrap{max-width:1080px;margin:0 auto;padding:24px 26px 60px}
h2{font-size:15px;letter-spacing:.02em;margin:30px 0 12px;color:var(--ink)}
.eyebrow{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 4px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}
.tile .v{font-size:25px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile .l{color:var(--ink2);font-size:12.5px;margin-top:4px}.tile .v.a{color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:14px}
.card h3{margin:0;padding:13px 16px;font-size:13.5px;border-bottom:1px solid var(--line);color:var(--ink2)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;padding:9px 14px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--line)}
td{padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px;background:rgba(240,106,98,.15);color:var(--crit);font-weight:700}
.k{font-size:12px;color:var(--ink2)}.k b{color:var(--crit)}
.note{color:var(--muted);font-size:13px;padding:14px 16px}
.dis{opacity:.6}
</style></head><body>
<div class=top><div class=glyph><span></span><span></span><span></span><span></span></div>
<div><b>ATAMŪRA CORE</b><small>КОКПИТ</small></div>
<span style="margin-left:auto;color:var(--muted);font-size:12px">читает снимки продуктов · логику не дублирует</span></div>
<div class=wrap>
  <p class=eyebrow>Финблок</p><h2 style="margin-top:2px">Финансовая сводка</h2>
  <div id=fin class=note>загрузка…</div>
  <p class=eyebrow style="margin-top:34px">Бот договоров</p><h2 style="margin-top:2px">🚩 Косяки инициаторов</h2>
  <div id=kos class=note>загрузка…</div>
</div>
<script>
var money=function(n){return (Math.round(n||0)).toLocaleString('ru-RU').replace(/,/g,' ');};
var esc=function(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');};
fetch('/api/finance').then(function(r){return r.json();}).then(function(d){
  var el=document.getElementById('fin');
  if(!d.connected){el.className='note';el.innerHTML='Финблок не подключён. '+esc(d.hint||d.error||'');return;}
  el.className='';
  el.innerHTML='<div class=tiles>'
    +tile('a',money(d.otток)+' ₸','Отток за '+(d.months||'?')+' мес')
    +tile('',money(d.приток)+' ₸','Приток')
    +tile((d.saldo>=0?'':'a'),(d.saldo>=0?'+':'')+money(d.saldo)+' ₸','Сальдо')
    +tile('',(d.matched==null?'—':d.matched),'Оплачено (матч 1С)')
    +tile('',(d.reserve==null?'—':d.reserve),'Ждёт 1С')
    +'</div>';
  function tile(a,v,l){return '<div class=tile><div class="v '+(a?'a':'')+'">'+v+'</div><div class=l>'+esc(l)+'</div></div>';}
}).catch(function(e){document.getElementById('fin').innerHTML='ошибка: '+e;});
fetch('/api/kosyaki').then(function(r){return r.json();}).then(function(d){
  var el=document.getElementById('kos');el.className='';
  var rows=d.rows||[];
  var head='<div class=tiles style="margin-bottom:14px">'
    +'<div class=tile><div class=v>'+d.total_zayavok+'</div><div class=l>заявок с генерацией</div></div>'
    +'<div class=tile><div class="v" style=color:var(--crit)>'+d.total_kosyakov+'</div><div class=l>всего косяков</div></div>'
    +'<div class=tile><div class=v>'+rows.length+'</div><div class=l>инициаторов</div></div></div>';
  if(!rows.length){el.innerHTML=head+'<div class=note>Логов генераций не найдено ('+esc(d.source)+'). Появятся, когда бот договоров сформирует договоры.</div>';return;}
  var body=rows.map(function(x){
    var kinds=(x.kinds||[]).map(function(k){return '<span class=k><b>'+k.n+'</b> '+esc(k.kind)+'</span>';}).join(' · ');
    return '<tr><td><b>'+esc(x.initiator)+'</b><div style="color:var(--muted);font-size:11px">id '+esc(x.initiator_id)+'</div></td>'
      +'<td class=num>'+x.zayavok+'</td><td class=num><span class=badge>'+x.kosyakov+'</span></td>'
      +'<td>'+esc(x.top_kind)+'</td><td>'+kinds+'</td></tr>';
  }).join('');
  el.innerHTML=head+'<div class=card><h3>Рейтинг по количеству косяков</h3>'
    +'<table><thead><tr><th>Инициатор</th><th class=num>Заявок</th><th class=num>Косяков</th><th>Топ-косяк</th><th>Разбивка</th></tr></thead><tbody>'+body+'</tbody></table></div>'
    +'<div class=note>Источник: логи генераций бота договоров ('+d.files+' файлов). Имена — из Bitrix (если задан вебхук), иначе ID.</div>';
}).catch(function(e){document.getElementById('kos').innerHTML='ошибка: '+e;});
</script></body></html>"""


if __name__ == "__main__":
    print(f"ATAMŪRA Core · кокпит → http://127.0.0.1:{PORT}")
    print(f"  договоры: {DOGOVOR_LOGS}")
    print(f"  финблок:  {FINANCE_URL or '(не задан FINANCE_URL)'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
