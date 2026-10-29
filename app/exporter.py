# -*- coding: utf-8 -*-
"""导出 Excel 明细、Markdown 报告和整包 ZIP。"""
import io
import json
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import store
from .analyzer import compute_stats, f1, fs, item_avg

FONT = "Arial"


def _load(jid):
    return store.get_job(jid), store.get_results(jid), store.get_analysis(jid)


def build_xlsx(jid):
    job, res, an = _load(jid)
    dims = an["dims"]
    wb = Workbook()
    ws = wb.active
    ws.title = "结果明细"
    head = ["问题编号", "第几次", "分类", "问题", "追问", "产品", "采集状态", "首字(秒)", "完成(秒)", "字数",
            *dims, "本条均分", "评分来源", "是否最佳", "标签", "人工备注", "AI点评", "模型/模式", "无痕",
            "截图文件", "回答文本", "提示/错误"]
    ws.append(head)
    by = {(r["query_id"], r["site"]): r for r in res["results"]}
    names = {s["key"]: s["name"] for s in res["sites"]}
    for q in res["queries"]:
        for s in res["sites"]:
            r = by.get((q["id"], s["key"]))
            it = an["items"].get(f"{q['id']}|{s['key']}") or {}
            a = item_avg(it, dims)
            ai = an["ai"].get(q["id"]) or {}
            ws.append([
                q.get("base_id") or q["id"], q.get("run", 1), q["category"], q["query"],
                " / ".join((q.get("turns") or [])[1:]), names[s["key"]],
                {"ok": "成功", "timeout": "超时", "error": "失败"}.get(r["status"], r["status"]) if r else "未采集",
                round(r["ttft_ms"] / 1000, 1) if r and r.get("ttft_ms") is not None else None,
                round(r["total_ms"] / 1000, 1) if r and r.get("total_ms") is not None else None,
                len(r.get("response_text") or "") if r else None,
                *[it.get("scores", {}).get(d) for d in dims],
                round(a, 2) if a is not None else None,
                ("AI" if it.get("src") == "ai" else "人工") if a is not None else None,
                "是" if an["best"].get(q["id"]) == s["key"] else None,
                "、".join(it.get("tags", [])) or None, it.get("note") or None,
                ai.get("comments", {}).get(s["key"]) or None,
                opt_text(r) if r else None,
                INCOG_NAMES.get(r.get("incognito"), r.get("incognito")) if r else None,
                r.get("screenshot") if r else None,
                (r.get("response_text") or "")[:32000] if r else None,
                (r.get("warning") or r.get("error")) if r else None,
            ])
    n = ws.max_row
    widths = {"问题": 40, "回答文本": 60, "AI点评": 40, "人工备注": 30, "截图文件": 30, "追问": 24}
    for i, h in enumerate(head, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 11)
    _style(ws)
    ws.freeze_panes = "E2"
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=head[c.column - 1] in ("问题", "AI点评", "人工备注"))

    # 汇总页：全部用公式引用明细，改明细里的分数会自动更新
    sm = wb.create_sheet("汇总", 0)
    col = {h: get_column_letter(i) for i, h in enumerate(head, 1)}
    rng = lambda h: f"结果明细!${col[h]}$2:${col[h]}${n}"
    sm.append([f"任务：{job['name']}", None, None, f"创建：{job['created_at']}"])
    sm.append(["评分为 1-5 分。本页数字均为公式，引用“结果明细”页，修改明细里的分数后会自动更新。"
               + (f"评测目的：{an['purpose']}" if an.get("purpose") else "")])
    sm.append([])
    hdr = ["产品", "采集条数", "成功条数", "成功率", "平均首字(秒)", "平均完成(秒)", "平均字数", "已评分", "综合均分", *dims, "最佳次数"]
    sm.append(hdr)
    start = sm.max_row + 1
    for i, s in enumerate(res["sites"]):
        r = start + i
        nm = f"$A{r}"
        p = rng("产品")
        sm.append([
            s["name"],
            f'=COUNTIFS({p},{nm},{rng("采集状态")},"<>未采集")',
            f'=COUNTIFS({p},{nm},{rng("采集状态")},"成功")',
            f'=IFERROR(C{r}/B{r},"—")',
            f'=IFERROR(AVERAGEIFS({rng("首字(秒)")},{p},{nm}),"—")',
            f'=IFERROR(AVERAGEIFS({rng("完成(秒)")},{p},{nm}),"—")',
            f'=IFERROR(AVERAGEIFS({rng("字数")},{p},{nm},{rng("采集状态")},"成功"),"—")',
            f'=COUNTIFS({p},{nm},{rng("本条均分")},"<>")',
            f'=IFERROR(AVERAGEIFS({rng("本条均分")},{p},{nm}),"—")',
            *[f'=IFERROR(AVERAGEIFS({rng(d)},{p},{nm}),"—")' for d in dims],
            f'=COUNTIFS({p},{nm},{rng("是否最佳")},"是")',
        ])
    end = sm.max_row
    for row in sm.iter_rows(min_row=start, max_row=end):
        for c in row[1:]:
            c.number_format = "0.0%" if c.column == 4 else ("0.0" if c.column >= 5 else "0")
    for i, h in enumerate(hdr, 1):
        sm.column_dimensions[get_column_letter(i)].width = 14 if i > 1 else 18
    for row in sm.iter_rows():
        for c in row:
            c.font = Font(name=FONT, bold=c.row in (1, start - 1), size=11 if c.row != 2 else 10,
                          color="666666" if c.row == 2 else "000000")
    for c in sm[start - 1]:
        c.fill = PatternFill("solid", fgColor="E3E8FB")

    cats = list(dict.fromkeys(q["category"] for q in res["queries"]))
    if len(cats) > 1:
        sm.append([])
        sm.append(["分类综合均分", *cats])
        hr = sm.max_row
        for s in res["sites"]:
            r = sm.max_row + 1
            sm.append([s["name"], *[f'=IFERROR(AVERAGEIFS({rng("本条均分")},{rng("产品")},$A{r},{rng("分类")},'
                                    f'{get_column_letter(j + 2)}${hr}),"—")' for j in range(len(cats))]])
            for c in sm[r][1:]:
                c.number_format = "0.0"
        for row in sm.iter_rows(min_row=hr):
            for c in row:
                c.font = Font(name=FONT, bold=c.row == hr)
        for c in sm[hr]:
            c.fill = PatternFill("solid", fgColor="E3E8FB")
    if an.get("report"):
        rp = wb.create_sheet("竞品结论", 1)
        rp.column_dimensions["A"].width = 110
        for para in an["report"].split("\n"):
            rp.append([para])
        for row in rp.iter_rows():
            for c in row:
                c.font = Font(name=FONT, size=11)
                c.alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _style(ws):
    for c in ws[1]:
        c.font = Font(name=FONT, bold=True)
        c.fill = PatternFill("solid", fgColor="E3E8FB")
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = Font(name=FONT)


def build_md(jid):
    job, res, an = _load(jid)
    st, cats = compute_stats(res, an)
    dims = an["dims"]
    ranked = sorted(st, key=lambda s: -(s["overall"] or -1))
    cfg = job["config"]
    L = [f"# {job['name']}", "",
         f"创建：{job['created_at']}　问题：{len(res['queries'])} 条　产品：{'、'.join(s['name'] for s in res['sites'])}　"
         + ("" if cfg.get("site_opts") is not None else f"模式：{MODE_NAMES.get(cfg.get('mode'), cfg.get('mode'))}　") +
         f"{'未登录浏览器' if cfg.get('profile') == 'guest' else ('无痕对话' if cfg.get('incognito') else '普通对话')}　设备：{'手机网页' if cfg.get('device') == 'mobile' else '电脑网页'}", ""]
    from .report import opt_desc
    sites_all = {s["key"]: s for s in store.get_sites()}
    desc = an.get("dim_desc") or {}
    L += ["## 评测设计", ""]
    if an.get("purpose"):
        L += [f"- 评测目的：{an['purpose']}"]
    L += ["- 产品与模型/模式：" + "；".join(f"{s['name']}（{opt_desc(cfg, s['key'], sites_all)}）" for s in res["sites"]),
          "- 评分维度（1–5 分）：" + "；".join(d + (f"：{desc[d]}" if desc.get(d) else "") for d in dims),
          "- 评分方式：AI 评审时隐去产品名并打乱顺序；人工打的分优先于 AI", ""]
    if an.get("report"):
        L += ["## 结论" if len(res["sites"]) == 1 else "## 竞品结论", "", an["report"], ""]
    L += ["## 综合得分", "", f"| 排名 | 产品 | 综合 | {' | '.join(dims)} | 最佳次数 | 已评 |",
          "|---|---|---|" + "---|" * len(dims) + "---|---|"]
    for i, s in enumerate(ranked, 1):
        L.append(f"| {i} | {s['name']} | {f1(s['overall'])} | " + " | ".join(f1(s["dim"][d]) for d in dims)
                 + f" | {s['wins']} | {s['scored']}/{s['n']} |")
    if len(cats) > 1:
        L += ["", "## 分类表现", "", f"| 产品 | {' | '.join(cats)} |", "|---|" + "---|" * len(cats)]
        for s in st:
            L.append(f"| {s['name']} | " + " | ".join(f1(s["cat"][c]) for c in cats) + " |")
    L += ["", "## 采集与速度", "", "| 产品 | 成功率 | 平均首字 | 平均完成 | 平均字数 |", "|---|---|---|---|---|"]
    for s in st:
        L.append(f"| {s['name']} | {'—' if s['ok_rate'] is None else str(round(s['ok_rate'] * 100)) + '%'} | "
                 f"{fs(s['ttft'])} | {fs(s['total'])} | {'—' if s['chars'] is None else round(s['chars'])} |")
    L += ["", "## 逐条记录", ""]
    by = {(r["query_id"], r["site"]): r for r in res["results"]}
    names = {s["key"]: s["name"] for s in res["sites"]}
    for q in res["queries"]:
        L += [f"### {q['id']}　{' → '.join(q.get('turns') or [q['query']])}", "", f"分类：{q['category']}"
              + (f"　最佳：{names.get(an['best'][q['id']])}" if an["best"].get(q["id"]) else ""), ""]
        ai = an["ai"].get(q["id"]) or {}
        if ai.get("summary") and len(res["sites"]) > 1:
            L += [f"AI 对比：{ai['summary']}", ""]
        for s in res["sites"]:
            r = by.get((q["id"], s["key"]))
            it = an["items"].get(f"{q['id']}|{s['key']}") or {}
            a = item_avg(it, dims)
            bits = [f"均分 {f1(a)}"]
            if r and r["status"] != "ok":
                bits.append({"timeout": "超时", "error": "采集失败"}.get(r["status"], r["status"]))
            if it.get("tags"):
                bits.append("标签：" + "、".join(it["tags"]))
            c = ai.get("comments", {}).get(s["key"])
            if c:
                bits.append("AI：" + c)
            if it.get("note"):
                bits.append("备注：" + it["note"].replace("\n", " "))
            if r and r.get("screenshot"):
                bits.append(f"截图：{r['screenshot']}")
            L.append(f"- **{s['name']}**：" + "；".join(bits))
        L.append("")
    return "\n".join(L)


OPT_OK = ("ok", "on", "off")


def opt_text(r):
    """模型/模式：想选的和实际是否选上。"""
    parts = [f"{o['label']}={'开' if o['want'] is True else '关' if o['want'] is False else o['want']}"
             + ("" if str(o["state"]).startswith(OPT_OK) else f"（没选上：{o['state']}）")
             for o in (r.get("options") or {}).values()]
    parts += [f"{k}:{v}" for k, v in (r.get("mode_state") or {}).items()]
    return "、".join(parts) or None


INCOG_NAMES = {None: "否", "url": "是", "on": "是", "on?": "是（未确认）", "guest": "未登录浏览器",
               "not_found": "否（没找到入口）", "not_configured": "否（未配置）", "click_failed": "否（点击失败）"}
MODE_NAMES = {"standard": "标准", "reasoning": "深度思考", "search": "联网搜索", "reasoning_search": "思考+搜索"}


def build_zip(jid, out_path):
    """直接写到磁盘文件，截图很多时也不会占满内存。"""
    base = store.job_dir(jid)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr("报告.md", build_md(jid))
        z.writestr("结果明细.xlsx", build_xlsx(jid))
        from .report import build_html, build_shots_xlsx
        z.writestr("报告.html", build_html(jid))
        z.writestr("截图对照.xlsx", build_shots_xlsx(jid))
        for p in base.rglob("*"):
            if p.is_file() and p.parts[len(base.parts)] not in ("live",) and not p.name.endswith(".tmp"):
                arc = p.relative_to(base).as_posix()
                if p.suffix.lower() in (".jpg", ".png"):
                    z.write(p, arc, compress_type=zipfile.ZIP_STORED)
                else:
                    z.write(p, arc)
    return out_path


def build_json(jid):
    res = store.get_results(jid)
    an = store.get_analysis(jid)
    return json.dumps({**res, "analysis": an}, ensure_ascii=False, indent=1).encode("utf-8")
