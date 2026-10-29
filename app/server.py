# -*- coding: utf-8 -*-
"""本地 Web 服务（只用标准库）。浏览器打开 http://127.0.0.1:8848 使用。"""
import argparse
import csv
import io
import json
import mimetypes
import re
import socket
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import analyzer, exporter, store
from .engine import Engine

STATIC = store.ROOT / "app" / "static"
engine = Engine()


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def job_view(jid, light=False):
    job = store.get_job(jid)
    if not job:
        raise ApiError("任务不存在", 404)
    job = dict(job)
    job["queue_pos"] = engine.queued.index(jid) + 1 if jid in engine.queued else None
    job["analysis"]["running"] = analyzer.is_running(jid)
    if light:
        job["config"] = {k: v for k, v in job["config"].items() if k != "queries"} | {
            "n_queries": len(job["config"]["queries"])}
    return job


def parse_upload(name, raw):
    name = name.lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        rows = [r for r in rows if any(c not in (None, "") for c in r)]
        return store.table_to_lines(rows)
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ApiError("文件编码无法识别，请另存为 UTF-8 的 CSV 或 Excel")
    if name.endswith(".csv"):
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        return store.table_to_lines(rows)
    if name.endswith(".json"):
        data = json.loads(text)
        lines = []
        for it in data:
            if isinstance(it, str):
                lines.append(it)
            elif isinstance(it, dict):
                q = it.get("query") or it.get("问题") or ""
                cat = it.get("category") or it.get("分类")
                fu = it.get("followups") or []
                lines.append((f"[{cat}] " if cat else "") + " || ".join([q, *fu]))
        return "\n".join(lines)
    return text


class Handler(BaseHTTPRequestHandler):
    server_version = "CompetitorBench/1.0"

    def log_message(self, *a):
        pass

    # ---------------- helpers
    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype, filename=None, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        if filename:
            q = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename=\"download\"; filename*=UTF-8''{q}")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype, filename):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        q = urllib.parse.quote(filename)
        self.send_header("Content-Disposition", f"attachment; filename=\"download\"; filename*=UTF-8''{q}")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def jbody(self):
        raw = self.body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "localhost")

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            if method != "GET" and not self.is_local() and not re.fullmatch(r"/api/jobs/[0-9a-z-]+/action", path):
                raise ApiError("手机/其他电脑上只能查看结果和处理提醒，其他操作请在运行平台的电脑上进行", 403)
            for m, pat, fn in ROUTES:
                if m != method:
                    continue
                mt = re.fullmatch(pat, path)
                if mt:
                    return fn(self, qs, *mt.groups())
            raise ApiError("找不到这个地址", 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json({"error": f"服务出错：{e}"}, 500)
            except Exception:
                pass

    # ---------------- static & files
    def index(self, qs):
        self.send_bytes((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

    def file(self, qs, jid, rel):
        base = store.job_dir(jid).resolve()
        p = (base / urllib.parse.unquote(rel)).resolve()
        if not str(p).startswith(str(base)) or not p.is_file():
            raise ApiError("文件不存在", 404)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_bytes(p.read_bytes(), ctype, cache="live/" not in rel)

    # ---------------- state
    def state(self, qs):
        import shutil
        try:
            free = shutil.disk_usage(store.DATA).free / 1e9
        except Exception:
            free = None
        self.send_json({"sites": store.get_sites(), "site_status": store.site_status(),
                        "settings": store.public_settings() if self.is_local() else {**store.public_settings(), "llm": {"has_key": store.get_settings()["llm"].get("api_key") != "", "model": store.get_settings()["llm"].get("model")}},
                        "engine": engine.status(), "lan": getattr(self.server, "lan_url", None),
                        "disk_free_gb": round(free, 1) if free is not None else None, "local": self.is_local()})

    def jobs(self, qs):
        out = store.list_jobs()
        for j in out:
            j["queue_pos"] = engine.queued.index(j["id"]) + 1 if j["id"] in engine.queued else None
        self.send_json(out)

    def job(self, qs, jid):
        self.send_json(job_view(jid, light="light" in qs))

    def job_full(self, qs, jid):
        job = job_view(jid)
        self.send_json({"job": job, "results": store.get_results(jid), "analysis": store.get_analysis(jid),
                        "log": store.tail_log(jid, 150)})

    def job_log(self, qs, jid):
        self.send_json({"log": store.tail_log(jid, 200)})

    def create(self, qs):
        try:
            job = store.create_job(self.jbody())
        except ValueError as e:
            raise ApiError(str(e))
        engine.submit(job["id"])
        self.send_json({"id": job["id"]})

    def preview(self, qs):
        b = self.jbody()
        items = store.parse_lines(b.get("text", ""))
        uniq, dup = store.dedupe(items)
        self.send_json({"count": len(uniq), "dup": dup, "multi": sum(1 for i in uniq if len(i["turns"]) > 1),
                        "cats": list(dict.fromkeys(i["category"] for i in uniq))})

    def action(self, qs, jid):
        b = self.jbody()
        a = b.get("action")
        job = store.get_job(jid)
        if not job:
            raise ApiError("任务不存在", 404)
        if not self.is_local() and a not in ("continue", "skip", "pause", "resume", "stop"):
            raise ApiError("这个操作请在运行平台的电脑上进行", 403)
        if a == "pause":
            engine.pause(jid)
        elif a == "resume":
            engine.resume(jid)
        elif a == "stop":
            engine.stop(jid)
        elif a == "retry":
            if engine.active == jid or jid in engine.queued:
                raise ApiError("任务还在进行中")
            engine.submit(jid)
        elif a == "continue":
            engine.continue_lane(jid, b.get("site"))
        elif a == "focus":
            if not self.is_local():
                raise ApiError("请在运行平台的电脑上操作", 403)
            if not engine.focus_lane(jid, b.get("site")):
                raise ApiError("这个产品的浏览器窗口已经关闭了")
        elif a == "skip":
            if not engine.skip_lane(jid, b.get("site")):
                raise ApiError("任务不在运行中")
        elif a == "rerun":
            if engine.active == jid or jid in engine.queued:
                raise ApiError("任务还在进行中，结束后再重新采集")
            keys = set(b.get("keys") or [])
            if not keys:
                raise ApiError("没有选择要重新采集的结果")
            with store._lock:
                data = store.get_results(jid)
                data["results"] = [r for r in data["results"] if f"{r['query_id']}|{r['site']}" not in keys]
                store.write_json(store.job_dir(jid) / "results.json", data)

                def upd(j):
                    res = data["results"]
                    j["progress"].update(done=len(res), ok=sum(1 for r in res if r["status"] == "ok"),
                                         failed=sum(1 for r in res if r["status"] in ("error", "timeout")))
                    for k, lane in j["lanes"].items():
                        lane["done"] = sum(1 for r in res if r["site"] == k)
                store.update_job(jid, upd)

                def drop_ai(an):
                    # 重新采集后旧的 AI 评分不再适用；人工打的分和备注保留
                    for k in list(an["items"]):
                        if k in keys and an["items"][k].get("src") == "ai":
                            del an["items"][k]
                store.update_analysis(jid, drop_ai)
            engine.submit(jid)
        elif a == "rename":
            store.update_job(jid, lambda j: j.update(name=(b.get("name") or j["name"]).strip()[:80]))
        elif a == "delete":
            if engine.active == jid:
                raise ApiError("任务正在运行，先停止再删除")
            if jid in engine.queued:
                engine.stop(jid)
            store.delete_job(jid)
        elif a == "analyze":
            if not analyzer.start_analysis(jid, only_missing=not b.get("all")):
                raise ApiError("分析已经在进行中")
        else:
            raise ApiError("未知操作")
        self.send_json({"ok": True})

    def analysis_op(self, qs, jid):
        b = self.jbody()
        op = b.get("op")

        def upd(a):
            if op == "item":
                it = b.get("item") or {}
                a["items"][b["key"]] = {"scores": {k: int(v) for k, v in (it.get("scores") or {}).items()
                                                   if isinstance(v, (int, float)) and 1 <= v <= 5},
                                        "tags": [str(t) for t in it.get("tags", [])][:20],
                                        "note": str(it.get("note", ""))[:5000], "src": it.get("src", "human")}
            elif op == "best":
                if b.get("site"):
                    a["best"][b["qid"]] = b["site"]
                else:
                    a["best"].pop(b["qid"], None)
            elif op == "report":
                a["report"] = str(b.get("text", ""))
            elif op == "dims":
                a["dims"], a["dim_desc"] = store.parse_dims(b.get("dims"), a["dims"])
                a["tags"] = store.parse_tags(b.get("tags"), a["tags"])
                if "purpose" in b:
                    a["purpose"] = str(b.get("purpose") or "").strip()[:2000]
            elif op == "reset_ai":
                a["items"] = {k: v for k, v in a["items"].items() if v.get("src") != "ai"}
                a["ai"] = {}
            else:
                raise ApiError("未知操作")
        self.send_json(store.update_analysis(jid, upd))

    def review(self, qs, jid):
        b = self.jbody()
        try:
            analyzer.review_one(jid, b["qid"])
        except analyzer.LLMError as e:
            raise ApiError(str(e))
        self.send_json(store.get_analysis(jid))

    def report(self, qs, jid):
        try:
            text = analyzer.write_report(jid)
        except analyzer.LLMError as e:
            raise ApiError(str(e))
        self.send_json({"report": text})

    def export(self, qs, jid, fmt):
        job = store.get_job(jid)
        if not job:
            raise ApiError("任务不存在", 404)
        name = re.sub(r'[\\/:*?"<>|]', "_", job["name"])[:60]
        if fmt == "xlsx":
            self.send_bytes(exporter.build_xlsx(jid),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{name}.xlsx")
        elif fmt == "md":
            self.send_bytes(exporter.build_md(jid).encode("utf-8"), "text/markdown; charset=utf-8", f"{name}.md")
        elif fmt == "html":
            from .report import build_html
            self.send_bytes(build_html(jid).encode("utf-8"), "text/html; charset=utf-8", f"{name}.html")
        elif fmt == "shots":
            from .report import build_shots_xlsx
            self.send_bytes(build_shots_xlsx(jid), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            f"{name}-截图对照.xlsx")
        elif fmt == "zip":
            tmpdir = store.DATA / "tmp"
            tmpdir.mkdir(exist_ok=True)
            p = tmpdir / f"{jid}-{threading.get_ident()}.zip"
            try:
                exporter.build_zip(jid, p)
                self.send_file(p, "application/zip", f"{name}.zip")
            finally:
                try:
                    p.unlink()
                except Exception:
                    pass
        elif fmt == "json":
            self.send_bytes(exporter.build_json(jid), "application/json", f"{name}.json")
        else:
            raise ApiError("未知格式")

    def open_folder(self, qs, jid):
        import subprocess
        d = store.job_dir(jid)
        sub = (qs.get("sub") or [""])[0]
        if sub in ("compare", "shots") and (d / sub).exists():
            d = d / sub
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
        self.send_json({"ok": True})

    # ---------------- sites
    def put_sites(self, qs):
        self.send_json(store.save_sites(self.jbody()))

    def site_login(self, qs, key):
        b = self.jbody()
        try:
            if b.get("action") == "close":
                engine.call(engine.close_login(key))
                threading.Thread(target=lambda: engine.call(engine.check_site(key), 120), daemon=True).start()
            else:
                engine.call(engine.open_login(key), 90)
        except Exception as e:
            raise ApiError(str(e).splitlines()[0][:200])
        self.send_json({"ok": True})

    def site_check(self, qs, key):
        def run():
            try:
                engine.call(engine.check_site(key), 120)
            except Exception as e:
                store.set_site_status(key, "error", str(e)[:150])
        threading.Thread(target=run, daemon=True).start()
        self.send_json({"ok": True})

    def site_probe(self, qs, key):
        b = self.jbody()
        question = (b.get("question") or "").strip()
        if not question:
            raise ApiError("请填写试跑用的问题")
        if key in engine.probing:
            raise ApiError("已经在试跑了")
        if key in engine.busy_sites:
            raise ApiError("这个产品正在采集中，等任务结束或暂停后再试跑")
        if not any(s["key"] == key for s in store.get_sites()):
            raise ApiError("产品不存在", 404)
        device = "mobile" if b.get("device") == "mobile" else "desktop"
        opts = {k: v for k, v in (b.get("opts") or {}).items() if v not in (None, "", "default")}

        def run():
            try:
                engine.call(engine.probe(key, question[:2000], device, opts), 1800)
            except Exception as e:
                traceback.print_exc()
                cur = store.get_probe(key) or {}
                if cur.get("state") == "running":
                    store.write_json(store.probe_dir(key) / "result.json",
                                     {**cur, "state": "error", "message": str(e).splitlines()[0][:300] if str(e) else "试跑失败"})
        threading.Thread(target=run, daemon=True).start()
        self.send_json({"ok": True})

    def site_probe_get(self, qs, key):
        r = store.get_probe(key)
        if r and r.get("state") == "running" and key not in engine.probing:
            r = {**r, "state": "error", "message": "试跑中断了（平台可能重启过），请重新试跑"}
        self.send_json(r or {"state": "none"})

    def scan_options(self, qs, key):
        if key in engine.probing or key in engine.busy_sites or key in engine.recording:
            raise ApiError("这个产品正在采集、试跑或录制，等结束后再更新选项")

        def run():
            try:
                engine.call(engine.scan_options(key), 900)
            except Exception as e:
                traceback.print_exc()
                store.write_json(store.probe_dir(key) / "options_scan.json", {"state": "error", "message": str(e).splitlines()[0][:300]})
        threading.Thread(target=run, daemon=True).start()
        self.send_json({"ok": True})

    def scan_options_get(self, qs, key):
        r = store.read_json(store.probe_dir(key) / "options_scan.json") or {"state": "none"}
        if r.get("state") == "running" and key not in engine.probing:
            r = {**r, "state": "error", "message": "读取中断了（平台可能重启过），请重新更新"}
        self.send_json(r)

    def site_options_op(self, qs, key):
        b = self.jbody()
        op = b.get("op")

        def clean_list(v):
            return [str(x).strip()[:40] for x in (v or []) if str(x).strip()][:30]

        def upd(cur):
            if op == "save_scan":
                sc = {}
                for oid, o in (b.get("options") or {}).items():
                    if isinstance(o.get("choices_by"), dict):
                        sc[oid] = {"choices_by": {str(k)[:20]: clean_list(v) for k, v in o["choices_by"].items() if clean_list(v)}, "found": True}
                    elif o.get("choices") is not None:
                        sc[oid] = {"choices": clean_list(o["choices"]), "found": True}
                cur["scanned"] = sc
                cur["scanned_at"] = store.now_iso()
            elif op == "reset":
                cur.pop("scanned", None)
                cur.pop("scanned_at", None)
            elif op == "add_custom":
                steps = [s for s in (b.get("steps") or []) if isinstance(s, dict)][:20]
                label = str(b.get("label") or "").strip()[:30]
                if not steps or not label:
                    raise ApiError("录制的步骤或名称是空的")
                cur.setdefault("custom", []).append({"id": "rec_" + store.new_job_id().replace("-", ""), "label": label,
                                                     "steps": steps, "created_at": store.now_iso()})
            elif op == "del_custom":
                cur["custom"] = [c for c in cur.get("custom") or [] if c["id"] != b.get("id")]
            else:
                raise ApiError("未知操作")
        store.update_site_options(key, upd)
        self.send_json({"site": next(s for s in store.get_sites() if s["key"] == key)})

    def site_record(self, qs, key):
        a = self.jbody().get("action")
        try:
            if a == "start":
                engine.call(engine.start_record(key), 90)
                self.send_json({"ok": True})
            elif a in ("stop", "cancel"):
                steps = engine.call(engine.stop_record(key), 60)
                self.send_json({"steps": steps if a == "stop" else []})
            else:
                raise ApiError("未知操作")
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(str(e).splitlines()[0][:200])

    def probe_file(self, qs, key, rel):
        base = store.probe_dir(key).resolve()
        p = (base / urllib.parse.unquote(rel)).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise ApiError("文件不存在", 404)
        self.send_bytes(p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    # ---------------- settings & library
    def put_settings(self, qs):
        store.save_settings(self.jbody())
        self.send_json(store.public_settings())

    def test_llm(self, qs):
        b = self.jbody()
        llm = b.get("llm") or {}
        if llm.get("api_key") == "__keep__":
            llm["api_key"] = store.get_settings()["llm"].get("api_key", "")
        try:
            secs = analyzer.test_llm(llm)
        except Exception as e:
            raise ApiError(str(e))
        self.send_json({"ok": True, "seconds": secs})

    def suggest_dims(self, qs):
        b = self.jbody()
        try:
            self.send_json(analyzer.suggest_dims(b.get("purpose", ""), b.get("text", "")))
        except analyzer.LLMError as e:
            raise ApiError(str(e))

    def library(self, qs):
        self.send_json(store.get_library())

    def put_library(self, qs):
        self.send_json(store.save_library(self.jbody()))

    def upload(self, qs):
        name = (qs.get("name") or ["file.txt"])[0]
        try:
            text = parse_upload(name, self.body())
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(f"读取文件失败：{e}")
        self.send_json({"text": text})


H = Handler
ROUTES = [
    ("GET", r"/", H.index),
    ("GET", r"/index\.html", H.index),
    ("GET", r"/files/([0-9a-z-]+)/(.+)", H.file),
    ("GET", r"/api/state", H.state),
    ("GET", r"/api/jobs", H.jobs),
    ("POST", r"/api/jobs", H.create),
    ("POST", r"/api/preview", H.preview),
    ("GET", r"/api/jobs/([0-9a-z-]+)", H.job),
    ("GET", r"/api/jobs/([0-9a-z-]+)/full", H.job_full),
    ("GET", r"/api/jobs/([0-9a-z-]+)/log", H.job_log),
    ("POST", r"/api/jobs/([0-9a-z-]+)/action", H.action),
    ("POST", r"/api/jobs/([0-9a-z-]+)/analysis", H.analysis_op),
    ("POST", r"/api/jobs/([0-9a-z-]+)/review", H.review),
    ("POST", r"/api/jobs/([0-9a-z-]+)/report", H.report),
    ("GET", r"/api/jobs/([0-9a-z-]+)/export/(xlsx|md|html|shots|zip|json)", H.export),
    ("POST", r"/api/jobs/([0-9a-z-]+)/open", H.open_folder),
    ("PUT", r"/api/sites", H.put_sites),
    ("POST", r"/api/sites/([a-z0-9_-]+)/login", H.site_login),
    ("POST", r"/api/sites/([a-z0-9_-]+)/check", H.site_check),
    ("POST", r"/api/sites/([a-z0-9_-]+)/probe", H.site_probe),
    ("GET", r"/api/sites/([a-z0-9_-]+)/probe", H.site_probe_get),
    ("GET", r"/probe-files/([a-z0-9_-]+)/(.+)", H.probe_file),
    ("POST", r"/api/sites/([a-z0-9_-]+)/scan-options", H.scan_options),
    ("GET", r"/api/sites/([a-z0-9_-]+)/scan-options", H.scan_options_get),
    ("POST", r"/api/sites/([a-z0-9_-]+)/options", H.site_options_op),
    ("POST", r"/api/sites/([a-z0-9_-]+)/record", H.site_record),
    ("PUT", r"/api/settings", H.put_settings),
    ("POST", r"/api/settings/test-llm", H.test_llm),
    ("POST", r"/api/suggest-dims", H.suggest_dims),
    ("GET", r"/api/library", H.library),
    ("PUT", r"/api/library", H.put_library),
    ("POST", r"/api/upload", H.upload),
]


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="竞品采集台")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--lan", action="store_true", help="允许同一局域网的手机/电脑访问（只读查看结果更方便）")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{args.port}"
    try:
        import urllib.request
        with urllib.request.urlopen(url + "/api/state", timeout=2) as r:
            if r.status == 200:
                print(f"竞品采集台已经在运行了，直接打开 {url}")
                webbrowser.open(url)
                return
    except Exception:
        pass
    store.DATA.mkdir(exist_ok=True)
    store.JOBS.mkdir(exist_ok=True)
    store.get_sites()
    store.get_library()
    engine.start()
    try:
        httpd = ThreadingHTTPServer((host, args.port), Handler)
    except OSError:
        sys.exit(f"端口 {args.port} 被其他程序占用了。可以用 --port 换一个端口启动")
    httpd.daemon_threads = True
    if args.lan and lan_ip():
        httpd.lan_url = f"http://{lan_ip()}:{args.port}"
    print("\n  竞品采集台已启动：" + url)
    if getattr(httpd, "lan_url", None):
        print("  同一 Wi-Fi 下的手机可访问：" + httpd.lan_url)
    print("  关闭这个窗口即可退出。\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("已退出")


if __name__ == "__main__":
    main()
