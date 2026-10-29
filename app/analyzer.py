# -*- coding: utf-8 -*-
"""自动分析：统计指标 + 大模型逐条评审 + 竞品结论。只用标准库发请求，不需要额外安装 SDK。"""
import base64
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from . import store

_running = set()
_rlock = threading.Lock()


# ---------------------------------------------------------------- LLM client
class LLMError(Exception):
    pass


def _post(url, headers, body, timeout=240):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(llm, prompt, images=None, max_tokens=4000):
    """images: list of JPEG bytes。返回模型回复文本。"""
    provider = llm.get("provider", "openai")
    if not llm.get("api_key") or not llm.get("model"):
        raise LLMError("还没有配置分析模型：到“设置”里填写接口地址、API Key 和模型名")
    images = images or []
    last = None
    for attempt in range(3):
        try:
            if provider == "anthropic":
                base = (llm.get("base_url") or "https://api.anthropic.com").rstrip("/")
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(b).decode()}} for b in images]
                content.append({"type": "text", "text": prompt})
                data = _post(base + "/v1/messages",
                             {"x-api-key": llm["api_key"], "anthropic-version": "2023-06-01"},
                             {"model": llm["model"], "max_tokens": max_tokens,
                              "messages": [{"role": "user", "content": content}]})
                return "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
            base = (llm.get("base_url") or "https://api.openai.com/v1").rstrip("/")
            if images:
                content = [{"type": "text", "text": prompt}] + [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
                    for b in images]
            else:
                content = prompt
            data = _post(base + "/chat/completions", {"Authorization": "Bearer " + llm["api_key"]},
                         {"model": llm["model"], "messages": [{"role": "user", "content": content}],
                          "temperature": 0.2, "max_tokens": max_tokens})
            return data["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            last = LLMError(f"接口返回 {e.code}：{detail}")
            if e.code in (429, 500, 502, 503, 504, 529):
                time.sleep(5 * (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = LLMError(f"连不上分析模型接口：{e}")
            time.sleep(3 * (attempt + 1))
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"接口返回的格式看不懂：{e}")
    raise last


def parse_json(text):
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    raise LLMError("模型没有按 JSON 格式回复")


def shot_bytes(path, width=800, max_h=7000):
    im = Image.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)), Image.LANCZOS)
    if im.height > max_h:
        im = im.crop((0, 0, im.width, max_h))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


# ---------------------------------------------------------------- stats
def item_avg(it, dims):
    if not it:
        return None
    v = [it["scores"][d] for d in dims if isinstance(it.get("scores", {}).get(d), (int, float))]
    return sum(v) / len(v) if v else None


def mean(a):
    a = [x for x in a if x is not None]
    return sum(a) / len(a) if a else None


def compute_stats(results, analysis):
    dims = analysis["dims"]
    queries = results["queries"]
    cats = list(dict.fromkeys(q["category"] for q in queries))
    by = {(r["query_id"], r["site"]): r for r in results["results"]}
    out = []
    for s in results["sites"]:
        k = s["key"]
        dim = {d: [] for d in dims}
        cat = {c: [] for c in cats}
        allv, tags, ttft, total, chars = [], {}, [], [], []
        ok = n = wins = 0
        for q in queries:
            r = by.get((q["id"], k))
            if r:
                n += 1
                if r["status"] == "ok":
                    ok += 1
                    chars.append(len(r.get("response_text") or ""))
                if r.get("ttft_ms") is not None:
                    ttft.append(r["ttft_ms"])
                if r.get("total_ms") is not None:
                    total.append(r["total_ms"])
            it = analysis["items"].get(f"{q['id']}|{k}")
            if it:
                for d in dims:
                    v = it.get("scores", {}).get(d)
                    if isinstance(v, (int, float)):
                        dim[d].append(v)
                a = item_avg(it, dims)
                if a is not None:
                    allv.append(a)
                    cat[q["category"]].append(a)
                for t in it.get("tags", []):
                    tags[t] = tags.get(t, 0) + 1
            if analysis["best"].get(q["id"]) == k:
                wins += 1
        out.append({"key": k, "name": s["name"], "overall": mean(allv), "scored": len(allv), "n": n, "ok": ok,
                    "dim": {d: mean(v) for d, v in dim.items()}, "cat": {c: mean(v) for c, v in cat.items()},
                    "wins": wins, "tags": tags, "ttft": mean(ttft), "total": mean(total), "chars": mean(chars),
                    "ok_rate": ok / n if n else None})
    return out, cats


def f1(v):
    return "—" if v is None else f"{v:.1f}"


def fs(msv):
    return "—" if msv is None else f"{msv / 1000:.1f}s"


# ---------------------------------------------------------------- per-query review
def build_review(results, q, analysis, use_images):
    """生成逐条评审的提示词。产品名换成匿名代号、顺序随机打乱，减少评审模型的品牌偏好和位置偏好。
    返回 (prompt, images, alias)，alias 是 代号 -> 真实 key。"""
    import random
    sites = list(results["sites"])
    random.shuffle(sites)
    alias = {chr(65 + i): s["key"] for i, s in enumerate(sites)}
    base = store.job_dir(results["run_id"])
    by = {r["site"]: r for r in results["results"] if r["query_id"] == q["id"]}
    dims, tags = analysis["dims"], analysis["tags"]
    question = " → 追问：".join(q.get("turns") or [q["query"]])
    dim_text = dims_prompt(analysis)
    purpose = purpose_prompt(analysis)
    images, shot_keys = [], []
    if use_images:
        for code, k in alias.items():
            r = by.get(k)
            if r and r.get("screenshot") and (base / r["screenshot"]).exists():
                try:
                    images.append(shot_bytes(base / r["screenshot"]))
                    shot_keys.append(code)
                except Exception:
                    pass
    blocks = []
    for code, k in alias.items():
        r = by.get(k)
        txt = (r.get("response_md") or r.get("response_text") or "")[:7000] if r else ""
        st = r["status"] if r else "无记录"
        blocks.append(f"### 回答 {code}\n采集状态：{st}\n{txt or '（没有提取到文本）'}")
    shot_note = ""
    if images:
        shot_note = (f"附带了 {len(images)} 张回答截图，顺序对应回答：{'、'.join(shot_keys)}。"
                     "文本由脚本从网页提取，可能混入少量界面文字，排版和格式以截图为准。\n")
    elif any(by.get(k, {}).get("response_md") for k in alias.values()):
        shot_note = "回答文本已按网页排版转成 Markdown（标题、列表、表格、代码块等），可以据此判断格式。\n"
    common = ("评分要求：只看回答本身的质量，不要因为回答长就给高分；事实错误、答非所问要明显扣分；"
              "问题确实有风险时的合理拒答不扣分，没必要的拒答或过度保守要扣分。")
    if len(sites) == 1:
        prompt = f"""你是资深的对话产品评测专家，请评价一个 chatbot 对下面问题的回答。

{purpose}评分维度（每项 1-5 分，5 最好，3 为及格）：
{dim_text}
{common}
可选标签（只能从中挑选，可以不选）：{'、'.join(tags)}
用户问题：{question}
{shot_note}
{blocks[0]}

只回复一个 JSON 对象，不要其他文字：
{{"scores":{{"<维度名>":1到5的整数}},"comment":"两三句中文，指出主要优点和问题","tags":["标签"]}}
维度名必须和上面完全一致。如果没有回答内容，scores 留空对象。"""
    else:
        prompt = f"""你是资深的对话产品评测专家。同一个用户问题被发给了 {len(sites)} 个不同的 chatbot，请横向对比它们的回答。各回答已匿名并随机排序。

{purpose}评分维度（每项 1-5 分，5 最好，3 为及格）：
{dim_text}
{common}
可选标签（只能从中挑选，可以不选）：{'、'.join(tags)}
用户问题：{question}
{shot_note}
{chr(10).join(blocks)}

只回复一个 JSON 对象，不要其他文字（键用回答代号 A、B、C…）：
{{"scores":{{"<代号>":{{"<维度名>":1到5的整数}}}},"best":"<代号>","summary":"两三句中文，用“回答A/回答B”指代，说清差异、哪个最好、为什么","comments":{{"<代号>":"一句中文点评，指出最主要的优点或问题"}},"tags":{{"<代号>":["标签"]}}}}
没有回答内容的不要出现在 scores 里。维度名必须和上面完全一致。"""
    return prompt, images, alias


def purpose_prompt(analysis):
    p = (analysis.get("purpose") or "").strip()
    return f"本次评测的目的：{p}\n请围绕这个目的评价，点评也要说清和目的相关的表现。\n\n" if p else ""


def dims_prompt(analysis):
    desc = analysis.get("dim_desc") or {}
    return "\n".join(f"- {d}" + (f"：{desc[d]}" if desc.get(d) else "") for d in analysis["dims"])


def suggest_dims(purpose, text="", llm=None):
    """按评测目的推荐评分维度和标签。"""
    llm = llm or store.get_settings()["llm"]
    purpose = (purpose or "").strip()
    if not purpose:
        raise LLMError("先写一下这次评测的目的")
    sample = [i["turns"][0] for i in store.parse_lines(text)][:15]
    prompt = f"""你在帮一位 chatbot 产品经理设计竞品评测方案。
评测目的：{purpose}
{("部分测试问题：" + chr(10).join("- " + s[:120] for s in sample)) if sample else ""}

请设计 4-6 个评分维度（每个维度 1-5 分打分，维度名 2-6 个字，评分标准一句话说清 5 分和 1 分分别是什么样），
以及 6-10 个评审时可以给回答打的标签（2-8 个字，正面和负面都要有）。维度要紧扣评测目的，互相不要重叠。
只回复 JSON：{{"dims":[{{"name":"维度名","desc":"评分标准"}}],"tags":["标签"]}}"""
    out = parse_json(chat(llm, prompt, max_tokens=1500))
    dims, desc = store.parse_dims(out.get("dims"), [])
    if not dims:
        raise LLMError("模型没有给出维度，换个说法再试一次")
    return {"dims": [{"name": d, "desc": desc.get(d, "")} for d in dims], "tags": store.parse_tags(out.get("tags"), [])}


def apply_review(jid, results, q, out, alias, overwrite_ai=True):
    sites = [s["key"] for s in results["sites"]]
    names = {s["key"]: s["name"] for s in results["sites"]}
    single = len(sites) == 1

    def real(d):
        return {alias.get(str(k).strip().upper(), k): v for k, v in (d or {}).items()} if isinstance(d, dict) else {}

    def unalias_text(t):
        t = str(t or "")
        for code, k in sorted(alias.items(), key=lambda x: -len(x[0])):
            t = re.sub(rf"回答\s*{code}(?![A-Za-z])", names[k], t)
        return t

    def upd(a):
        dims, allowed = a["dims"], set(a["tags"])
        if single:
            k = sites[0]
            per = {k: out.get("scores") or {}}
            comments = {k: str(out.get("comment") or "")}
            tags = {k: out.get("tags") or []}
            summary = str(out.get("comment") or "")
        else:
            per = real(out.get("scores"))
            comments = {k: unalias_text(v) for k, v in real(out.get("comments")).items()}
            tags = real(out.get("tags"))
            summary = unalias_text(out.get("summary"))
        a["ai"][q["id"]] = {"summary": summary, "comments": comments}
        for k in sites:
            sc = per.get(k)
            if not isinstance(sc, dict) or not sc:
                continue
            key = f"{q['id']}|{k}"
            it = a["items"].get(key)
            if it and item_avg(it, dims) is not None and it.get("src") != "ai":
                continue
            if it and it.get("src") == "ai" and not overwrite_ai:
                continue
            scores = {}
            for d in dims:
                try:
                    v = int(round(float(sc.get(d))))
                    if 1 <= v <= 5:
                        scores[d] = v
                except (TypeError, ValueError):
                    pass
            note = (it or {}).get("note", "")
            a["items"][key] = {"scores": scores, "tags": [t for t in (tags.get(k) or []) if t in allowed],
                               "note": note, "src": "ai"}
        b = alias.get(str(out.get("best") or "").strip().upper())
        if not single and b in sites and not a["best"].get(q["id"]):
            a["best"][q["id"]] = b
    store.update_analysis(jid, upd)


def review_one(jid, qid, llm=None):
    llm = llm or store.get_settings()["llm"]
    results = store.get_results(jid)
    analysis = store.get_analysis(jid)
    q = next(x for x in results["queries"] if x["id"] == qid)
    if not any(r["query_id"] == qid for r in results["results"]):
        raise LLMError("这个问题还没有采集结果")
    prompt, images, alias = build_review(results, q, analysis, bool(llm.get("vision")))
    out = parse_json(chat(llm, prompt, images))
    apply_review(jid, results, q, out, alias)


def start_analysis(jid, only_missing=True):
    with _rlock:
        if jid in _running:
            return False
        _running.add(jid)
    threading.Thread(target=_run_analysis, args=(jid, only_missing), daemon=True).start()
    return True


def is_running(jid):
    return jid in _running


def _run_analysis(jid, only_missing):
    try:
        llm = store.get_settings()["llm"]
        results = store.get_results(jid)
        analysis = store.get_analysis(jid)
        have = {r["query_id"] for r in results["results"] if r["status"] in ("ok", "timeout")}
        todo = []
        for q in results["queries"]:
            if q["id"] not in have:
                continue
            if only_missing:
                done = all(item_avg(analysis["items"].get(f"{q['id']}|{s['key']}"), analysis["dims"]) is not None
                           or not any(r["query_id"] == q["id"] and r["site"] == s["key"] for r in results["results"])
                           for s in results["sites"])
                if done and q["id"] in analysis["ai"]:
                    continue
            todo.append(q["id"])
        state = {"done": 0, "failed": 0, "last_error": ""}
        store.update_job(jid, lambda j: j.update(analysis={"state": "running", "done": 0, "total": len(todo),
                                                           "failed": 0, "message": "AI 正在逐条评审"}))

        def work(qid):
            try:
                review_one(jid, qid, llm)
                state["done"] += 1
            except Exception as e:
                state["failed"] += 1
                state["last_error"] = str(e)[:200]
                store.log(jid, "分析", f"{qid} 评审失败：{e}")
            store.update_job(jid, lambda j: j["analysis"].update(done=state["done"], failed=state["failed"]))

        if todo:
            if not llm.get("api_key"):
                raise LLMError("还没有配置分析模型：到“设置”里填写接口地址、API Key 和模型名")
            with ThreadPoolExecutor(max_workers=max(1, min(int(llm.get("concurrency") or 2), 6))) as ex:
                list(ex.map(work, todo))
        msg = f"评审完成 {state['done']} 条" + (f"，失败 {state['failed']} 条：{state['last_error']}" if state["failed"] else "")
        store.update_job(jid, lambda j: j["analysis"].update(state="writing", message="正在写竞品结论"))
        try:
            write_report(jid, llm)
        except Exception as e:
            msg += f"；结论生成失败：{e}"
        store.update_job(jid, lambda j: j["analysis"].update(state="done", message=msg, finished_at=store.now_iso()))
        from .engine import notify
        notify("分析完成", msg)
    except Exception as e:
        store.update_job(jid, lambda j: j["analysis"].update(state="error", message=str(e)[:300]))
    finally:
        _running.discard(jid)


def pick_questions(results, analysis, n=8):
    """最值得看的问题：多产品取各家均分差距最大的，单产品取得分最低的。返回 [{q, spread, lo, scores}]。"""
    dims = analysis["dims"]
    keys = [s["key"] for s in results["sites"]]
    out = []
    for q in results["queries"]:
        sc = {k: item_avg(analysis["items"].get(f"{q['id']}|{k}"), dims) for k in keys}
        v = [x for x in sc.values() if x is not None]
        if not v:
            continue
        out.append({"q": q, "spread": max(v) - min(v) if len(v) > 1 else None, "lo": min(v), "scores": sc})
    if len(keys) > 1:
        out = sorted((p for p in out if p["spread"] is not None), key=lambda p: -p["spread"])
    else:
        out.sort(key=lambda p: p["lo"])
    return out[:n]


# 内置分析框架：AI 按这个结构写结论；数据表格、排名、截图由系统自动附在报告里
REPORT_FRAMEWORK_MULTI = """请用 Markdown 写，按下面的结构，每节用“### ”开头作小标题（不要写一级、二级标题；不要画表格，数据表系统会自动附上）：
### 一句话结论
一两句话直接回答评测目的；没有写目的时，说清谁整体最好、差距主要在哪。
### 核心发现
3-5 条，按重要性排序。每条先给结论，再给证据：引用问题编号、分数或分差、典型标签。
### 各产品画像
每个产品一小段：整体水平与定位、最强和最弱的维度或分类、一个典型例子（问题编号）。
### 典型案例
从“差距最大的问题”里挑 2-4 个最能说明问题的，说清各家怎么答的、好在哪、差在哪。
### 值得借鉴与建议
面向我们自己的产品团队，3-5 条可以执行的建议，每条说明依据（来自哪些发现或案例）。
### 局限与下一步
样本量、题目覆盖、评测方式（AI 评审、网页采集）带来的局限，以及建议补测什么。"""

REPORT_FRAMEWORK_SINGLE = """请用 Markdown 写，按下面的结构，每节用“### ”开头作小标题（不要写一级、二级标题；不要画表格，数据表系统会自动附上）：
### 一句话结论
一两句话直接回答评测目的；没有写目的时，概括整体水平和最大的问题。
### 核心发现
3-5 条，按重要性排序。每条先给结论，再给证据：引用问题编号、分数、典型标签。
### 强项与短板
结合分维度、分类得分，说清最强和最弱的地方，各举一个例子（问题编号）。
### 典型案例
从“得分最低的问题”里挑 2-4 个，说清问题出在哪、好的回答应该是什么样。
### 改进建议
3-5 条可以执行的建议，每条说明依据。
### 局限与下一步
样本量、题目覆盖、评测方式带来的局限，以及建议补测什么。"""


def report_prompt(results, analysis):
    st, cats = compute_stats(results, analysis)
    dims = analysis["dims"]
    single = len(results["sites"]) == 1
    lines = []
    for s in st:
        lines.append(f"- {s['name']}：综合 {f1(s['overall'])}；" + "，".join(f"{d} {f1(s['dim'][d])}" for d in dims)
                     + f"；最佳 {s['wins']} 次；平均首字 {fs(s['ttft'])}，完成 {fs(s['total'])}，"
                     + f"平均 {'—' if s['chars'] is None else round(s['chars'])} 字；采集成功 {s['ok']}/{s['n']}；"
                     + "标签：" + ("、".join(f"{t}×{c}" for t, c in s["tags"].items()) or "无"))
    cat_lines = ""
    if len(cats) > 1:
        cat_lines = "\n分类综合分：\n" + "\n".join(f"- {s['name']}：" + "，".join(f"{c} {f1(s['cat'][c])}" for c in cats)
                                              for s in st)
    notes = []
    names = {s["key"]: s["name"] for s in results["sites"]}
    for q in results["queries"]:
        parts = []
        for s in results["sites"]:
            it = analysis["items"].get(f"{q['id']}|{s['key']}")
            c = (analysis["ai"].get(q["id"]) or {}).get("comments", {}).get(s["key"])
            if it and it.get("note"):
                parts.append(f"{s['name']}（人工）：{it['note']}")
            elif c and single:
                parts.append(c)
        summ = (analysis["ai"].get(q["id"]) or {}).get("summary")
        if parts or (summ and not single):
            b = analysis["best"].get(q["id"])
            notes.append(f"【{q['id']} {q['category']}｜{q['query'][:40]}】" + (f"最佳：{names.get(b, b)}；" if b else "")
                         + (f"对比：{summ}；" if summ and not single else "") + "；".join(parts))
    picks = []
    for p in pick_questions(results, analysis, 8):
        q = p["q"]
        ai = analysis["ai"].get(q["id"]) or {}
        picks.append(f"【{q['id']}｜{q['query'][:50]}】" + "，".join(f"{names[k]} {f1(v)}" for k, v in p["scores"].items())
                     + (f"；AI 对比：{ai['summary']}" if ai.get("summary") and not single else "")
                     + ("".join(f"；{names.get(k, k)}：{c}" for k, c in (ai.get("comments") or {}).items()) if single else ""))
    picks_text = (("\n得分最低的问题：\n" if single else "\n差距最大的问题：\n") + "\n".join(picks)) if picks else ""
    task = ("请写一份 800-1300 字的" + ("单产品评测结论" if single else "竞品调研结论") + "，面向产品团队，可以直接贴进周报。\n"
            + (REPORT_FRAMEWORK_SINGLE if single else REPORT_FRAMEWORK_MULTI))
    purpose = (analysis.get("purpose") or "").strip()
    if purpose:
        task = f"这次评测的目的是：{purpose}。结论要先直接回答这个目的，再展开。" + task
    return f"""你在帮一位 chatbot 产品经理写竞品调研结论。下面是 {len(results['queries'])} 条测试问题的评测汇总（评分 1-5 分）。
评分维度：
{dims_prompt(analysis)}

{chr(10).join(lines)}{cat_lines}

{picks_text}

逐条记录（节选）：
{chr(10).join(notes[:150]) or '（无）'}

{task}
只用上面给出的数据，不要编造；引用数字时保留一位小数。"""


def write_report(jid, llm=None):
    llm = llm or store.get_settings()["llm"]
    results = store.get_results(jid)
    analysis = store.get_analysis(jid)
    if not any(item_avg(it, analysis["dims"]) is not None for it in analysis["items"].values()):
        raise LLMError("还没有任何评分，先完成逐条评审")
    text = chat(llm, report_prompt(results, analysis), max_tokens=4500).strip()
    text = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", text)
    store.update_analysis(jid, lambda a: a.update(report=text, report_at=store.now_iso()))
    return text


def test_llm(llm):
    t0 = time.time()
    out = chat(llm, '只回复 JSON：{"ok":true}', max_tokens=50)
    parse_json(out)
    return round(time.time() - t0, 1)
