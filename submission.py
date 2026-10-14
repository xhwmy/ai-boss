"""Complete a BOSS application through the site's visible chat controls.

The site enables attachment delivery only after both sides have replied.  The
workflow records each confirmed phase so restarting never repeats a message.
"""

import json
import time
from resume_upload import get_uploaded_meta


CHAT_URL = "https://www.zhipin.com/web/geek/chat"
def greeting_for(resume, record):
    """Mention one concrete résumé strength, then relevant JD skill overlap."""
    title = (record.get("job_title") or "这个岗位").strip()[:35]
    jd = (record.get("jd_text") or "").lower()
    ordered = ["Linux", "Docker", "K8S", "Nginx", "MySQL", "Oracle", "Shell",
               "Prometheus", "Grafana", "GitLab", "Java", "Vue"]
    skills = set()
    for values in resume.get("skills", {}).values():
        if isinstance(values, list):
            skills.update(str(x).lower() for x in values)
    matched = [x for x in ordered if x.lower() in jd and x.lower() in skills][:2]
    name = (resume.get("name") or "求职者").strip()
    highlights = resume.get("highlights") or []
    if not isinstance(highlights, list):
        highlights = []
    candidates = [" ".join(str(item).split()).strip("。； ") for item in highlights
                  if isinstance(item, str) and item.strip()]
    # Prefer a concrete project/operations result over a generic skill list.
    proof = next((item for item in candidates if any(word in item.lower() for word in
                  ("独立", "部署", "监控", "自动化", "ci/cd", "运维"))), "")
    if proof:
        proof = proof[:75].rstrip("、，； ")
        advantage = f"我的相关实践：{proof}。"
    elif matched:
        advantage = f"我的简历包含{'、'.join(matched)}相关技能。"
    else:
        advantage = "希望结合我的项目经历进一步交流。"
    extra = [skill for skill in matched if skill.lower() not in proof.lower()]
    overlap = f"也熟悉岗位提到的{'、'.join(extra)}。" if proof and extra else ""
    return f"您好，我是{name}，想应聘贵司的{title}。{advantage}{overlap}期待进一步沟通，谢谢！"


class SubmissionWorkflow:
    def __init__(self, wb, ledger, log):
        self.wb = wb
        self.ledger = ledger
        self.log = log

    def _js(self, code, data=None):
        if data is not None:
            code = code.replace("__DATA__", json.dumps(data, ensure_ascii=False))
        return self.wb.evaluate(code)

    def open_conversation(self, record):
        company = (record.get("company") or "").strip()
        title = (record.get("job_title") or "").strip()
        if not company or not title:
            return False, "缺少公司或岗位名称，无法安全定位会话"
        # Reuse an existing chat tab. Navigating a loaded chat SPA repeatedly
        # can detach Webbridge for long enough to lose the pending message.
        reused = self.wb.switch_tab("/web/geek/chat")
        if not reused:
            self.wb.navigate(CHAT_URL, new_tab=True)
            time.sleep(3)
        deadline = time.time() + 75
        while time.time() < deadline:
            try:
                count = self._js("document.querySelectorAll('li[role=listitem] .friend-content').length") or 0
            except Exception:
                time.sleep(1)
                continue
            if count:
                break
            time.sleep(0.5)
        else:
            return False, "聊天页未加载或浏览器桥接超时"
        # BOSS often adds a newly contacted recruiter to the chat list several
        # seconds after the contact button succeeds. The existing chat tab can
        # also keep an old list until it is reloaded. Wait, then refresh once.
        picked = {}
        for refresh in (False, True):
            if refresh:
                self.wb.navigate_fast(CHAT_URL)
                time.sleep(2)
            deadline = time.time() + (20 if not refresh else 40)
            while time.time() < deadline:
                try:
                    picked = self._js("""(() => {
                        const name = __DATA__;
                        const matches=[...document.querySelectorAll('li[role=listitem] .friend-content')]
                            .filter(e=>e.innerText.includes(name));
                        if(matches.length!==1) return {count:matches.length};
                        matches[0].click(); return {count:1};
                    })()""", company) or {}
                except Exception:
                    time.sleep(1)
                    continue
                if picked.get("count") == 1 or picked.get("count", 0) > 1:
                    break
                time.sleep(1)
            if picked.get("count"):
                break
        if picked.get("count") != 1:
            return False, f"会话匹配数为 {picked.get('count', 0)}，需人工核对"
        deadline = time.time() + 12
        while time.time() < deadline:
            state = self._js("""(() => ({
                ready:!!document.querySelector('#chat-input'),
                text:document.querySelector('.chat-conversation')?.innerText.slice(0,250)||''
            }))()""") or {}
            if state.get("ready") and company in state.get("text", ""):
                if title not in state.get("text", ""):
                    return False, "会话岗位与待投递岗位不一致"
                return True, ""
            time.sleep(0.4)
        return False, "会话内容未加载"

    def _greeting_state(self, text):
        return self._js("""(() => {const msg=__DATA__;
            const e=[...document.querySelectorAll('.chat-conversation .message-item.item-myself')]
                .find(x=>x.querySelector('.text-content')?.innerText.trim()===msg);
            if(!e)return 'absent';
            const s=e.querySelector('.message-status');
            return s&&/已发送|送达|已读/.test(s.innerText)?'sent':'unconfirmed';
        })()""", text) or "absent"

    def send_greeting(self, record, text):
        if self.ledger.phase(record["url"], "greeting_sent"):
            return "sent", "已记录"
        visible = self._greeting_state(text)
        if visible == "sent":
            self.ledger.mark_phase(record, "greeting_sent")
            return "sent", "页面已有相同消息"
        if visible == "unconfirmed":
            return "unconfirmed", "页面已有相同消息，但状态未确认"
        draft = self._js("document.querySelector('#chat-input')?.innerText || ''") or ""
        if draft.strip() and draft.strip() != text:
            return "failed", "聊天输入框已有草稿，未覆盖"
        self.wb.call("cdp", {"method": "Page.bringToFront", "params": {}})
        try:
            if draft.strip() != text:
                self.wb.mouse_click("#chat-input")
                self.wb.type_text(text)
            state = {}
            for _ in range(8):
                state = self._js("""(() => ({text:document.querySelector('#chat-input')?.innerText||'',
                    disabled:document.querySelector('button.btn-send')?.classList.contains('disabled')}))()""") or {}
                if state.get("text", "").strip() == text and not state.get("disabled"):
                    break
                time.sleep(0.25)
            if state.get("text", "").strip() != text or state.get("disabled"):
                return "failed", "输入框内容或发送按钮状态未通过核对"
            self.wb.mouse_click("button.btn-send")
            deadline = time.time() + 15
            while time.time() < deadline:
                visible = self._greeting_state(text)
                if visible == "sent":
                    self.ledger.mark_phase(record, "greeting_sent")
                    return "sent", "已在会话中确认"
                time.sleep(0.5)
            return "unconfirmed", "点击发送后未看到消息回显"
        finally:
            try:
                self.wb.call("cdp", {"method": "Emulation.setFocusEmulationEnabled", "params": {"enabled": False}})
            except Exception:
                pass

    def send_resume(self, record):
        resume_name = (get_uploaded_meta() or {}).get("filename", "")
        if not resume_name:
            return "failed", "本地控制面板尚未上传简历附件"
        if self.ledger.phase(record["url"], "resume_sent"):
            return "sent", "已记录"
        existing = self._js("""(() => [...document.querySelectorAll('.chat-conversation .message-item.item-system')]
            .some(e=>e.innerText.includes(__DATA__)&&e.innerText.includes('已发送给Boss')))()""", resume_name)
        if existing:
            self.ledger.mark_phase(record, "resume_sent")
            return "sent", "页面已有简历发送回执"
        if self.ledger.phase(record["url"], "resume_attempted"):
            return "unconfirmed", "曾尝试发送，但未核对到回执；需要人工查看会话"
        state = self._js("""(() => {const e=[...document.querySelectorAll('.chat-conversation .toolbar-btn')]
            .find(x=>x.innerText.trim()==='发简历');return {exists:!!e,unable:!!e?.classList.contains('unable'),
            hint:e?.getAttribute('aria-label')||''}})()""") or {}
        if not state.get("exists"):
            return "failed", "未找到发简历按钮"
        if state.get("unable"):
            return "pending", state.get("hint") or "等待招聘方回复"
        self._js("""(() => {const e=[...document.querySelectorAll('.chat-conversation .toolbar-btn')]
            .find(x=>x.innerText.trim()==='发简历');e?.click();return !!e})()""")
        time.sleep(0.7)
        modal = self._js("""(() => {const m=document.querySelector('.dialog-wrap.active .choose-resume-dialog');
            return {open:!!m,names:m?[...m.querySelectorAll('.resume-name')].map(e=>e.innerText.trim()):[]}})()""") or {}
        if resume_name not in modal.get("names", []):
            return "failed", f"BOSS 附件列表中没有 {resume_name}"
        self._js("""(() => {const m=document.querySelector('.dialog-wrap.active .choose-resume-dialog');
            const e=[...m.querySelectorAll('.resume-list .list-item')]
                .find(x=>x.querySelector('.resume-name')?.innerText.trim()===__DATA__);
            e?.click();return !!e})()""", resume_name)
        time.sleep(0.5)
        enabled = self._js("""(() => {const b=document.querySelector('.dialog-wrap.active button.btn-confirm');
            return !!b&&!b.disabled&&!b.classList.contains('disabled')})()""")
        if not enabled:
            return "failed", "简历已选中但发送按钮未启用"
        before_ids = self._js("""(() => [...document.querySelectorAll('.chat-conversation .message-item.item-system')]
            .filter(e=>e.innerText.includes('附件简历')&&e.innerText.includes('已发送给Boss'))
            .map(e=>e.getAttribute('data-mid')))()""") or []
        self.ledger.mark_phase(record, "resume_attempted")
        self._js("document.querySelector('.dialog-wrap.active button.btn-confirm').click()")
        deadline = time.time() + 15
        while time.time() < deadline:
            found = self._js("""(() => [...document.querySelectorAll('.chat-conversation .message-item.item-system')]
                .some(e=>e.innerText.includes('附件简历')&&e.innerText.includes('已发送给Boss')
                    && !__DATA__.includes(e.getAttribute('data-mid'))))()""", before_ids)
            if found:
                self.ledger.mark_phase(record, "resume_sent")
                return "sent", "BOSS 显示已发送给 Boss"
            time.sleep(0.5)
        return "unconfirmed", "点击发送后没有简历回执"

    def process(self, record, greeting):
        try:
            ok, reason = self.open_conversation(record)
            if not ok:
                return "failed", reason
            status, reason = self.send_greeting(record, greeting)
            if status != "sent":
                return status, f"个性化招呼：{reason}"
            status, reason = self.send_resume(record)
            return status, f"附件简历：{reason}"
        except Exception as exc:
            return "unconfirmed", f"浏览器操作未确认：{exc}；下轮先检查会话回显"
