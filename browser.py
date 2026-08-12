"""
浏览器自动化模块

通过 KIMI Webbridge WebSocket 协议控制 Edge 浏览器。
所有操作走扩展的 chrome.debugger API（CDP 级别），零自动化检测。

核心策略：
  - 默认已登录，不反复检查登录状态
  - 搜索岗位 → 获取详情页 URL → 直接导航到每个详情页
  - 投递后关闭聊天弹窗，回到搜索页继续下一个
  - 翻页时重新搜索
"""

import json
import re
import time
from pathlib import Path

from config import ROOT_DIR
from utils import random_delay, setup_logger
from webbridge_client import WebbridgeClient, WebbridgeError

logger = setup_logger("browser")

# ── BOSS 直聘页面 URL ──
BOSS_URL = "https://www.zhipin.com"
SEARCH_URL = "https://www.zhipin.com/web/geek/job"
LOGIN_URL = "https://www.zhipin.com/web/user/"

# ── 页面文字标记 ──
APPLIED_MARKER = "已沟通"
CHAT_SEND = "发送"
JD_DESC_MARKERS = ["职位描述", "岗位职责", "工作内容", "工作职责", "岗位描述"]
JD_REQUIRE_MARKERS = ["任职要求", "岗位要求", "职位要求", "我们希望", "岗位需求"]
PAGE_NEXT = "下一页"

# ── 岗位失效标记（打开详情后被 BOSS 重定向到失效页时页面出现的文字）──
JOB_EXPIRED_MARKERS = [
    "职位已失效", "该职位已下线", "职位已下线", "岗位已下线",
    "岗位不存在", "职位不存在", "很抱歉，该职位",
]

# ── 学历 / 经验 文本兜底匹配（JS DOM 提取失败时用于扫描页面文字）──
EDUCATION_PATTERNS = ["学历不限", "大专", "本科", "硕士", "博士", "高中", "中专", "中技", "初中"]
EXPERIENCE_PATTERNS = ["经验不限", "在校/应届", "应届生", "无需经验",
                       "1年以内", "1-3年", "3-5年", "5-10年", "10年以上"]

# ── BOSS 网页内置筛选（经验/学历/薪资/类型），label 对应页面筛选栏文字 ──
FILTER_CATEGORIES = {
    "experience": {
        "label": "经验",
        "options": ["不限", "在校/应届", "经验不限", "1年以内", "1-3年", "3-5年", "5-10年", "10年以上"],
    },
    "education": {
        "label": "学历",
        "options": ["不限", "大专", "本科", "硕士", "博士"],
    },
    "salary": {
        "label": "薪资",
        "options": ["不限", "3K以下", "3-5K", "5-10K", "10-20K", "20-50K", "50K以上"],
    },
    "job_type": {
        "label": "类型",
        "options": ["不限", "全职", "兼职", "实习"],
    },
}

# ── 定位左侧岗位列表的滚动目标与滚轮落点坐标 ──
# ⭐ v2.9：BOSS 新版搜索页左侧岗位列表不再是独立滚动容器——实测
# `.job-list-container` / `.rec-job-list` 的 scrollHeight==clientHeight、
# overflowY:visible，整页（窗口）滚动才能触发懒加载。旧代码「通用可滚动容器
# 兜底」会命中右侧详情栏 `.job-detail-container`（内部可滚动且含相似职位链接），
# 滚轮落在详情栏上只滚动详情内容、永远不会滚动窗口/左列表，懒加载永不触发——
# 这就是「完成一批后卡住、滚轮条在最右侧」的根因。
# 修复：① 只认左侧列表容器（左 60% 视口内且含岗位链接）；② 左列表本身可独立
# 滚动则滚容器，否则滚窗口（滚轮落在左列表列内，事件冒泡到窗口即整页滚动 +
# 触发 BOSS 懒加载）；③ 绝不把右侧详情栏当滚动目标。
SCROLL_INFO_JS = """
    (() => {
        const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
        const hasJobLink = el => !!el.querySelector('a[href*="job_detail"]');
        // 1. 左侧岗位列表容器（只看左侧列、可见、且含岗位链接的）
        const leftCands = [...document.querySelectorAll(
            '.job-list-container, .rec-job-list, .job-list-box, '
            + '[class*="job-list"], [class*="job_list"], [class*="jobList"], '
            + '[class*="search-job"]'
        )].filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && r.left < innerWidth * 0.6
                && hasJobLink(el);
        });
        // 2. 左侧列表本身可独立滚动（有独立滚动条）→ 滚动该容器
        const scrollable = leftCands.find(c => c.scrollHeight > c.clientHeight + 30);
        if (scrollable) {
            const rect = scrollable.getBoundingClientRect();
            return {
                found: true, container: true,
                scrollTop: scrollable.scrollTop,
                scrollHeight: scrollable.scrollHeight,
                clientHeight: scrollable.clientHeight,
                x: clamp(Math.round(rect.left + Math.min(rect.width * 0.5, 300)), 80, Math.round(innerWidth - 60)),
                y: clamp(Math.round(rect.top + Math.min(rect.height * 0.4, 400)), 120, Math.round(innerHeight - 60)),
            };
        }
        // 3. 当前布局：左侧列表不独立滚动 → 整页（窗口）滚动。
        //    滚轮落点取左列表列内（事件冒泡到窗口即整页滚动，触发 BOSS 懒加载）。
        if (leftCands.length) {
            const rect = leftCands[0].getBoundingClientRect();
            return {
                found: true, container: false,
                scrollTop: window.scrollY,
                scrollHeight: document.body ? document.body.scrollHeight : 0,
                clientHeight: window.innerHeight,
                x: clamp(Math.round(rect.left + Math.min(rect.width * 0.5, 200)), 80, Math.round(innerWidth * 0.6)),
                y: clamp(Math.round(rect.top + Math.min(rect.height * 0.25, 300)), 120, Math.round(innerHeight - 60)),
            };
        }
        // 4. 兜底：窗口滚动，落点取视口左侧 1/4（BOSS 列表列）
        return {
            found: true, container: false,
            scrollTop: window.scrollY,
            scrollHeight: document.body ? document.body.scrollHeight : 0,
            clientHeight: window.innerHeight,
            x: clamp(Math.round(innerWidth * 0.25), 150, 450),
            y: clamp(Math.round(innerHeight * 0.7), 200, Math.round(innerHeight - 40)),
        };
    })()
"""

# ── 岗位详情页结构化信息提取（标题/薪资/公司/学历/经验/城市/类型）──
# ⭐ v2.6：优先取 BOSS 网页的语义化 DOM（h1.name / .salary / .company-name / .job-info 标签）。
# 之前用纯文本启发式选标题，会把「经验1-3年」「本科」等顶部标签行误当成岗位名称，
# 导致人工审核表格显示的是工作经验要求而不是岗位名称。
# ⭐ v2.8：BOSS 新版详情页结构变化——① 公司名不再从 .company-info .name 取（会命中
# 「岗位标题+薪资」的隐藏模板），改为取招聘者卡片 .boss-info-attr「公司名·人事职位」的
# 「·」前缀；② 城市/经验/学历改用 .text-desc（text-city/text-experiece/text-degree），
# 旧 .job-info .text 已消失，裸 [class*="tag"] 会误抓「待优化」徽标。
EXTRACT_META_JS = r"""
    (() => {
        const txt = el => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '');
        const r = { title:'', salary:'', company:'', education:'', experience:'', city:'', job_type:'',
                    company_size:'', financing:'', listed:'', industry:'' };

        // 1. 岗位标题：只在「职位信息区」内找，绝不整页扫 .name
        //    ⚠️ BOSS 详情页的公司名也是 .name（如 .company-info .name / h3.name），
        //    且公司块可能排在职位块之前——旧代码用整页 querySelector('... , .name')，
        //    会把公司名当成标题，导致「岗位名称 = 公司名称」。
        const jobScopes = ['.info-primary', '.job-primary', '.job-banner', '.job-sec', '.job-detail'];
        let titleEl = null;
        for (const s of jobScopes) {
            titleEl = document.querySelector(
                s + ' h1.name, ' + s + ' h1, ' + s + ' .name h1, '
                + s + ' .job-title, ' + s + ' [class*="job-title"]'
            );
            if (titleEl) break;
        }
        if (!titleEl) titleEl = document.querySelector('h1, .job-title, [class*="job-title"]');
        if (titleEl) r.title = txt(titleEl).slice(0, 60);

        // 2. 薪资
        const salaryEl = document.querySelector('.salary, [class*="salary"]');
        if (salaryEl) r.salary = txt(salaryEl).slice(0, 30);

        // 3. 公司名称：来自招聘者卡片（人事信息）前缀。
        //    ⚠️ 旧代码用 .company-info .name 等 .name 后代找公司名，但 BOSS 详情页里
        //    .company-info .name 命中的是「岗位标题+薪资」的隐藏模板（实测文本形如
        //    「某岗位（某区域分部） 15-21K」），导致公司名 = 岗位名+薪资。
        //    真实来源：BOSS 把公司名放在招聘者卡片人事信息的前缀，形如
        //    「示例公司·人事招聘专员」→ 公司名 = 「·」前的「示例公司」。
        const isVisible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const notHrLine = t => !/专员|经理|招聘|人事|HR|hr|招聘者|刚刚|活跃/.test(t);
        let company = '';
        // 3a. 首选：招聘者卡片 .boss-info-attr 的「·」前缀（公司名在前缀）
        for (const el of [...document.querySelectorAll('.boss-info-attr, [class*="boss-info-attr"]')]) {
            if (!isVisible(el)) continue;
            const p = txt(el).split(/[·・]/)[0].trim();
            if (p && p.length <= 40 && notHrLine(p)) { company = p; break; }
        }
        // 3b. 兜底：公司信息区短公司名（.sider-company 内的 .company-info）
        if (!company) {
            const infoEl = document.querySelector('.sider-company [class*="company-info"]');
            if (infoEl) {
                const t = txt(infoEl);
                if (t && t.length <= 40 && notHrLine(t)) company = t;
            }
        }
        // 3c. 兜底：可见的 .company-name（去掉「公司名称」标签）。
        //     相似职位区也有 .company-name（隐藏的），必须排除在相似职位容器内的元素。
        if (!company) {
            for (const el of [...document.querySelectorAll('.company-name, [class*="company-name"]')]) {
                if (!isVisible(el)) continue;
                let p = el.parentElement, inSimilar = false;
                for (let k = 0; k < 5 && p; k++, p = p.parentElement) {
                    const c = (p.className && p.className.toString()) || '';
                    if (/similar|recommend|sug|推荐|相似/i.test(c)) { inSimilar = true; break; }
                }
                if (inSimilar) continue;
                const t = txt(el).replace(/^公司名称/, '').trim();
                if (t && t.length <= 60 && notHrLine(t)) { company = t; break; }
            }
        }
        r.company = company.slice(0, 60);

        // 4. 经验/学历/城市/类型 标签
        //    ⭐ v2.8：BOSS 新版详情页用 .text-desc 元素展示城市/经验/学历
        //    （class 含 text-city / text-experiece（官网拼写）/ text-degree），
        //    旧版 .job-info .text 结构已不出现；且裸 [class*="tag"] 会误抓「待优化」等
        //    徽标当城市。① 先按 class 精确归类（新版）；② 再兜底标签集合（兼容老布局）。
        const textDescEls = [...document.querySelectorAll('.text-desc, [class*="text-desc"]')];
        const pickByClass = cls => {
            const el = textDescEls.find(e => ((e.className || '').toString().includes(cls)));
            return el ? txt(el) : '';
        };
        const cityFromCls = pickByClass('text-city');
        if (cityFromCls && cityFromCls.length <= 14) r.city = cityFromCls;
        let expFromCls = pickByClass('text-experi') || pickByClass('text-exp');
        if (expFromCls) {
            if (expFromCls === '不限') expFromCls = '经验不限';
            if (expFromCls.length <= 14) r.experience = expFromCls;
        }
        const eduFromCls = pickByClass('text-degree');
        if (eduFromCls) r.education = eduFromCls;

        // 兜底标签集合（老布局 .job-info .text 等；不用裸 [class*="tag"]，避免抓徽标）
        const tags = [];
        const pushTag = t => {
            t = (t || '').trim();
            if (t && t.length <= 20 && !tags.includes(t)) tags.push(t);
        };
        document.querySelectorAll('.job-info .text, .job-info p, .tag-list span, .job-tag').forEach(el => {
            if (el.children.length > 3) return;
            const t = txt(el);
            if (t.includes(' ') && t.length > 10) {
                t.split(/[·\s]+/).forEach(x => pushTag(x));   // 整段多标签则拆分
            } else {
                pushTag(t);
            }
        });

        const isExp  = /^(经验不限|不限|在校\/应届|应届生|无需经验|[1-9]\d*年以内|[1-9]-\d+年|10年以上)$/;
        const isEdu  = /^(学历不限|大专|本科|硕士|博士|高中|中专|中技|初中)$/;
        const isType = /^(全职|兼职|实习)$/;
        const isCity = /^[一-龥·\-]{1,14}$/;

        for (const t of tags) {
            if (!r.experience && isExp.test(t)) r.experience = (t === '不限') ? '经验不限' : t;
            else if (!r.experience) {
                const tt = t.replace(/^经验/, '');
                if (isExp.test(tt)) r.experience = tt;
            }
            else if (!r.education && isEdu.test(t)) r.education = t;
            else if (!r.job_type && isType.test(t)) r.job_type = t;
            else if (!r.city && isCity.test(t) && !isEdu.test(t) && !isType.test(t) && !/[年Kk]/.test(t)) r.city = t;
        }

        // 类型兜底：新版布局类型不单独成标签，扫 .text-desc / .job-status 区域
        if (!r.job_type) {
            const typeSrc = [...textDescEls, ...document.querySelectorAll('.job-status, [class*="job-status"]')]
                .map(el => txt(el)).join(' ');
            const tm = typeSrc.match(/(全职|兼职|实习)/);
            if (tm) r.job_type = tm[1];
        }

        // 5. 公司规模 / 融资上市 / 所属行业
        //    ⭐ v3.0：给「符合预期/超预期」岗位提供详细信息（公司规模、是否上市等）。
        //    先扫公司信息区，再整页文本兜底（BOSS 公司卡文本形如「100-499人 · 已上市 · 互联网」）。
        const SIZE_RE = /(少于15人|15-50人|50-150人|150-500人|500-2000人|2000人以上|\d+-\d+人|\d+人以上)/;
        const FIN_RE = /(已上市|未上市|拟上市|不需要融资|未融资|天使轮|Pre-A轮|A\+轮|A轮|B\+轮|B轮|C\+轮|C轮|D轮及以上|D轮|E轮|战略融资)/;
        const INDUSTRY_RE = /(移动互联网|计算机软件|人工智能|大数据|物联网|信息安全|数据服务|企业服务|云计算|互联网|电子商务|游戏|硬件|半导体|通信|电子|金融|证券|保险|银行|医疗|医药|教育|培训|新能源|汽车|机械制造|机械|制造|物流|广告|传媒|房地产|建筑|餐饮|消费|机器人|自动驾驶)/;
        const area = document.querySelector(
            '.sider-company, [class*="sider-company"], [class*="company-info"], [class*="job-sec"]'
        ) || document.body;
        const areaText = txt(area);
        const wholeText = (document.body ? document.body.innerText : '') || '';
        const pick = re => { const m = areaText.match(re) || wholeText.match(re); return m ? m[1] : ''; };
        if (!r.company_size) r.company_size = pick(SIZE_RE);
        if (!r.financing) {
            const fin = pick(FIN_RE);
            if (fin) {
                r.financing = fin;
                // 是否上市：已上市=是；出现其他融资阶段/未融资=未上市；没识别到=未知
                r.listed = (fin === '已上市') ? true
                    : (/轮|未上市|拟上市|未融资|不需要融资/.test(fin) ? false : '');
            }
        }
        if (!r.industry) r.industry = pick(INDUSTRY_RE);
        return r;
    })()
"""


class BossBrowser:
    """BOSS 直聘浏览器控制器（基于 KIMI Webbridge）"""

    def __init__(self, config: dict):
        self.config = config
        self.wb = WebbridgeClient()
        self._last_search_url = ""  # 记住搜索结果页 URL，用于投递后返回

    # ══════════ 浏览器启动与关闭 ══════════

    def start(self):
        """启动 WebSocket 服务器，等待 KIMI 扩展连接"""
        logger.info("🌐 正在启动 KIMI Webbridge 连接...")
        self.wb.start()
        if not self.wb.wait_ready(timeout=60):
            raise ConnectionError(
                "KIMI Webbridge 扩展未连接。请确保：\n"
                "  1. Edge 浏览器已打开\n"
                "  2. KIMI Webbridge 扩展已安装并启用"
            )
        logger.info("✅ KIMI Webbridge 就绪！")

    def close(self):
        """关闭连接"""
        try:
            self.wb.close()
            logger.info("🔌 Webbridge 连接已关闭")
        except Exception as e:
            logger.warning(f"关闭连接时出错: {e}")

    # ══════════ 页面等待辅助 ══════════

    def _wait_for_text(self, text: str, timeout: float = 10) -> bool:
        """等待页面出现指定文本"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.wb.page_has_text(text):
                    return True
            except WebbridgeError:
                pass
            time.sleep(0.5)
        return False

    def _wait_page_ready(self, timeout: float = 10):
        """等待页面内容加载（body 非空）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                text = self.wb.get_page_text()
                if len(text.strip()) > 50:
                    return True
            except WebbridgeError:
                pass
            time.sleep(0.5)
        return False

    def _wait_url_contains(self, fragment: str, timeout: float = 10) -> bool:
        """等待 URL 包含指定片段"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                url = self.wb.get_page_url()
                if fragment in url:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _ensure_tab_on_boss(self):
        """确保操作在 BOSS 直聘页面上（默认已登录）

        ⭐ 绝不替换当前标签页：若当前页是控制面板等其他页面且没有 BOSS 标签页，
        会在「新标签页」中打开 BOSS，原标签页（控制面板）完整保留。
        """
        # 1. 当前标签页已经是 BOSS → 直接使用
        try:
            url = self.wb.get_page_url()
            if url and "zhipin.com" in url:
                return True
        except Exception:
            pass

        # 2. 已存在 BOSS 标签页 → 切换过去（避免重复开标签页）
        try:
            tabs = self.wb.list_tabs()
            boss_tabs = [t for t in tabs if "zhipin" in t.get("url", t.get("title", ""))]
            if boss_tabs and self.wb.switch_tab("zhipin.com"):
                time.sleep(1)
                return True
        except Exception:
            pass

        # 3. 都没有 → 在「新标签页」打开 BOSS（不替换当前标签页，控制面板才不会丢失）
        logger.info("  🆕 正在新标签页打开 BOSS 直聘...")
        try:
            self.wb.navigate(BOSS_URL, new_tab=True)
            time.sleep(2)
            # 确保后续操作落在 BOSS 标签页上（扩展打开新标签后可能不自动激活）
            if not self._activate_boss_tab():
                time.sleep(1)
                self._activate_boss_tab()
        except WebbridgeError as e:
            logger.warning(f"  ⚠️ 扩展新标签打开失败（{e}），改用 JS 打开新标签页")
            try:
                self.wb.evaluate("window.open('https://www.zhipin.com', '_blank')")
                time.sleep(2)
                self._activate_boss_tab()
            except Exception:
                pass

        # 最终校验：确保当前标签页已是 BOSS
        # （否则后续导航会把控制面板等当前标签页替换掉）
        try:
            url = self.wb.get_page_url()
            if url and "zhipin.com" in url:
                self._wait_page_ready(timeout=15)
                return True
        except Exception:
            pass
        raise ConnectionError(
            "无法在新标签页打开 BOSS 直聘（当前标签页不是 BOSS 页面）。\n"
            "请确认 Edge 浏览器已打开、KIMI Webbridge 扩展已启用后重试。"
        )

    def _activate_boss_tab(self) -> bool:
        """激活已存在的 BOSS 标签页（最多重试 3 次，等待新标签页注册）"""
        for _ in range(3):
            try:
                if self.wb.switch_tab("zhipin.com"):
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    # ══════════ 登录（简化） ══════════

    def _is_on_login_page(self) -> bool:
        """检查是否需要登录（URL 跳转或页面内弹层）"""
        try:
            url = self.wb.get_page_url()
            if "passport" in url or "login" in url:
                return True
            # BOSS 可能在当前页弹出登录层而不跳转 URL
            text = self.wb.get_page_text()
            if "请登录" in text[:1000]:
                return True
            return False
        except Exception:
            return False

    def ensure_logged_in(self):
        """确保已登录。如果发现页面跳转到登录页，提示用户扫码。"""
        self._ensure_tab_on_boss()

        if not self._is_on_login_page():
            logger.info("  ✅ 登录状态正常")
            return

        # 发现登录页，需要登录
        logger.info("📱 检测到未登录，请在浏览器中扫码登录...")
        logger.info("   等待登录完成（最长 5 分钟）...")
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(3)
            if not self._is_on_login_page():
                logger.info("  ✅ 登录成功！")
                return
            remaining = int(deadline - time.time())
            if remaining % 30 == 0:
                logger.info(f"   等待中（还剩 {remaining} 秒）...")
        logger.warning("  ⚠️ 登录超时，请手动登录后重启")

    # ══════════ 搜索功能 ══════════

    def search_jobs(self, keyword: str, city: str = "", filters: dict = None) -> bool:
        """搜索岗位并返回是否成功。

        filters: BOSS 网页内置筛选条件，如 {"experience": "在校/应届", "education": "本科"}。
                搜索后用网页原生下拉筛选智能过滤岗位。
        """
        # 1. 直接导航到搜索页
        search_url = SEARCH_URL
        if city:
            search_url += f"?city={city}"
        logger.info(f"  🔍 正在搜索: {keyword}")
        self.wb.navigate(search_url)
        time.sleep(3)
        self._wait_page_ready(timeout=15)

        # 2. 检查是否跳转到登录页
        if self._is_on_login_page():
            logger.warning("  ⚠️ 需要先登录")
            return False

        # 3. 点击搜索框输入关键词
        selectors = [
            "input[placeholder*='搜索']",
            "input[placeholder*='职位']",
            "input[type='text']",
        ]
        clicked = False
        for sel in selectors:
            try:
                self.wb.mouse_click(sel)
                clicked = True
                break
            except WebbridgeError:
                continue

        if not clicked:
            logger.warning("  ⚠️ 未找到搜索框")
            return False

        # 4. 输入关键词并回车
        time.sleep(0.5)
        self.wb.type_text(keyword)
        logger.info(f"  ⌨️ 已输入: {keyword}")
        time.sleep(0.5)
        self.wb.send_keys("Enter")
        logger.info("  🔍 正在搜索...")

        # 5. 等待结果
        time.sleep(3)
        self._wait_page_ready(timeout=10)

        # 6. 应用 BOSS 网页内置筛选（经验/学历/薪资/类型）
        if filters:
            self.apply_filters(filters)

        # 7. 记住当前 URL（含筛选参数，用于投递后返回保持筛选状态）
        try:
            self._last_search_url = self.wb.get_page_url()
        except Exception:
            pass

        # 8. 检查是否有结果
        page_text = self.wb.get_page_text()
        if "立即沟通" in page_text:
            logger.info("  ✅ 搜索成功")
            return True
        if len(page_text) > 200:
            logger.info("  ✅ 页面已加载")
            return True
        logger.warning("  ⚠️ 搜索结果页异常")
        return False

    # ══════════ 网页内置筛选功能（智能查询） ══════════

    def apply_filters(self, filters: dict):
        """使用 BOSS 网页内置的筛选下拉框过滤岗位（经验/学历/薪资/类型）"""
        for key, option in (filters or {}).items():
            cat = FILTER_CATEGORIES.get(key)
            if not cat or not option or str(option).strip() in ("", "不限"):
                continue
            label = cat["label"]
            if str(option) not in cat["options"]:
                logger.warning(f"  ⚠️ 忽略无效筛选值 {label}={option}")
                continue
            logger.info(f"  🎚️ 设置筛选: {label}={option}")
            if self._apply_filter(label, str(option)):
                time.sleep(2)  # 等筛选结果刷新
            else:
                logger.warning(f"  ⚠️ 筛选 {label}={option} 应用失败，继续（不影响后续操作）")

    def _apply_filter(self, label: str, option: str) -> bool:
        """点击筛选栏下拉按钮，再选择目标选项。返回是否成功。"""
        # 第一步：打开对应筛选下拉
        open_js = f"""
            (() => {{
                const scope = document.querySelector(
                    '.search-tool, [class*="filter-bar"], [class*="search-bar"], [class*="search-tool"]'
                ) || document.body;
                const cands = [...scope.querySelectorAll('div,span,button,li')].filter(el => {{
                    const t = (el.textContent || '').trim();
                    return t === {json.dumps(label)};
                }}).filter(el => el.children.length <= 2 && el.offsetHeight >= 10 && el.offsetWidth >= 10);
                if (!cands.length) return false;
                cands.sort((a,b) => a.offsetWidth * b.offsetHeight - b.offsetWidth * a.offsetHeight);
                const btn = cands[0];
                btn.scrollIntoView({{block:'center', behavior:'instant'}});
                btn.dispatchEvent(new MouseEvent('click', {{bubbles:true, cancelable:true}}));
                return true;
            }})()
        """
        try:
            opened = bool(self.wb.evaluate(open_js))
        except WebbridgeError:
            opened = False
        if not opened:
            return False
        time.sleep(1)

        # 第二步：在下拉弹层里点击目标选项
        select_js = f"""
            (() => {{
                const opt = {json.dumps(option)};
                const all = [...document.querySelectorAll('div,span,li,a,p')].filter(el => {{
                    const t = (el.textContent || '').trim();
                    return t === opt;
                }}).filter(el => {{
                    try {{ return el.offsetHeight >= 10 && el.checkVisibility(); }} catch(e) {{ return false; }}
                }});
                if (!all.length) return false;
                // 优先选下拉弹层/面板内的选项，避免点到岗位卡片上的相同文字
                const inPopup = all.filter(el => {{
                    let p = el.parentElement;
                    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {{
                        const c = (p.className && p.className.toString()) || '';
                        if (/drop|pop|menu|select|option|panel/i.test(c)) return true;
                    }}
                    return false;
                }});
                const pool = inPopup.length ? inPopup : all;
                pool.sort((a,b) => a.offsetWidth * b.offsetHeight - b.offsetWidth * a.offsetHeight);
                const el = pool[0];
                el.dispatchEvent(new MouseEvent('click', {{bubbles:true, cancelable:true}}));
                return true;
            }})()
        """
        try:
            return bool(self.wb.evaluate(select_js))
        except WebbridgeError:
            return False

    def get_job_urls(self) -> list[str]:
        """从搜索结果页提取所有岗位详情页的 URL

        ⭐ v2.5：只保留真正的岗位详情链接（/job_detail/<id>.html），
        过滤掉 job_detail/（无岗位 ID）这类会被 BOSS 当作失效页跳转的
        链接，避免引擎把失效页当岗位处理、页面卡在 job_detail/ 上。
        """
        js = r"""
            (() => {
                const links = [...document.querySelectorAll('a[href*="job_detail"]')];
                const urls = new Set();
                for (const a of links) {
                    const h = a.href || '';
                    const idx = h.indexOf('/job_detail/');
                    if (idx < 0) continue;
                    const seg = h.slice(idx + '/job_detail/'.length).split(/[\/?#&]/)[0];
                    if (seg && /\.html$/i.test(seg)) urls.add(h);
                }
                return [...urls];
            })()
        """
        try:
            urls = self.wb.evaluate(js) or []
            if urls:
                logger.info(f"  📋 获取到 {len(urls)} 个岗位 URL")
            return urls
        except Exception as e:
            logger.warning(f"获取岗位 URL 失败: {e}")
            return []

    # ══════════ 左侧列表下拉加载更多岗位 ══════════

    def _count_job_links(self) -> int:
        """统计当前页面已渲染的岗位链接数量"""
        try:
            val = self.wb.evaluate(
                "document.querySelectorAll('a[href*=\"job_detail\"]').length"
            )
            return int(val or 0)
        except Exception:
            return 0

    def _scroll_left_list(self) -> bool:
        """滚动左侧岗位列表到底部触发懒加载，返回滚动位置是否变化。

        ⭐ v2.4：改为发送「真实鼠标滚轮」事件（CDP Input.dispatchMouseEvent
        mouseWheel，isTrusted=True）。BOSS 直聘的懒加载/下拉刷新只响应真实的
        wheel 输入——之前用 JS 设置 scrollTop + 合成 scroll 事件
        （isTrusted=False）不会被识别，导致只能读取第一批岗位、无法往下刷。
        ⭐ v2.5：连续滚轮直到列表到底（scrollTop+clientHeight≥scrollHeight），
        最多 12 次，每 2 次检查一次位置与是否到底，确保滚到列表最底部，
        而不是只滚一小段就停（只滚一小段触发不了 BOSS 的懒加载）。
        ⭐ v2.9：修复「完成一批后卡住、滚轮条在最右侧」。BOSS 新版搜索页左侧
        岗位列表已不是独立滚动容器，整页（窗口）滚动才会触发懒加载；而旧
        SCROLL_INFO_JS 的通用兜底会误选右侧详情栏 `.job-detail-container`
        （内部可滚动且含相似职位链接），滚轮只滚动详情内容，窗口/左列表不动，
        懒加载永不触发。v2.9 的 SCROLL_INFO_JS 只认左侧列表容器：可独立滚动则
        滚容器，否则滚窗口（滚轮落在左列表列内，冒泡到窗口即整页滚动）。
        """
        # 1. 定位可滚动容器，取滚动信息与滚轮落点坐标
        try:
            info = self.wb.evaluate(SCROLL_INFO_JS) or {}
        except Exception:
            info = {}
        if not info.get("found"):
            return False

        before = info.get("scrollTop", 0)
        x, y = info.get("x", 300), info.get("y", 400)

        # 2. 连续发送真实滚轮事件（向下），模拟人手滚动触发 BOSS 懒加载
        moved = False
        bottom_coast = 0  # 到达底部后再多滚几次，等懒加载回流
        for i in range(12):
            try:
                self.wb.mouse_wheel(delta_y=600, x=x, y=y)
            except Exception:
                break
            time.sleep(0.25)
            if i % 2 == 1:  # 每滚动 2 次检查一次位置变化
                try:
                    cur_info = self.wb.evaluate(SCROLL_INFO_JS) or {}
                    cur = cur_info.get("scrollTop", before)
                    if cur != before:
                        moved = True
                    before = cur
                    # 已滚到目标底部 → 再补几次滚轮（等懒加载回流）后结束本轮
                    ch = cur_info.get("clientHeight", 0)
                    sh = cur_info.get("scrollHeight", 0)
                    if ch and sh and cur + ch >= sh - 10:
                        bottom_coast += 1
                        if bottom_coast >= 2:
                            for _ in range(2):
                                try:
                                    self.wb.mouse_wheel(delta_y=600, x=x, y=y)
                                    time.sleep(0.3)
                                except Exception:
                                    pass
                            break
                except Exception:
                    pass

        # 3. 容器坐标未生效 → 尝试视口左侧 1/4 处（BOSS 列表所在列，可能容器定位偏差）
        if not moved:
            try:
                vw = int(self.wb.evaluate("window.innerWidth") or 1280)
                vh = int(self.wb.evaluate("window.innerHeight") or 800)
                cx = max(150, min(int(vw * 0.25), 450))
                cy = max(200, int(vh * 0.5))
                for _ in range(8):
                    self.wb.mouse_wheel(delta_y=600, x=cx, y=cy)
                    time.sleep(0.25)
                cur = (self.wb.evaluate(SCROLL_INFO_JS) or {}).get("scrollTop", before)
                moved = cur != before
            except Exception:
                pass

        if moved:
            return True
        return self._scroll_left_list_js_fallback()

    def _scroll_left_list_js_fallback(self) -> bool:
        """JS 兜底：直接设置滚动位置到最底并触发 scroll 事件（真实滚轮失效时才用）

        ⭐ v2.9：同样只认左侧列表容器，左侧列表不可独立滚动时滚窗口；
        绝不滚动右侧详情栏（旧代码通用兜底会误选 `.job-detail-container`）。
        """
        js = """
            (() => {
                const hasJobLink = el => !!el.querySelector('a[href*="job_detail"]');
                const leftCands = [...document.querySelectorAll(
                    '.job-list-container, .rec-job-list, .job-list-box, '
                    + '[class*="job-list"], [class*="job_list"], [class*="jobList"], '
                    + '[class*="search-job"]'
                )].filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.left < innerWidth * 0.6
                        && hasJobLink(el);
                });
                const target = leftCands.find(c => c.scrollHeight > c.clientHeight + 30) || null;
                if (target) {
                    const top = target.scrollTop;
                    target.scrollTop = target.scrollHeight;
                    target.dispatchEvent(new Event('scroll', {bubbles:true}));
                    return target.scrollTop !== top;
                }
                // 左侧列表不可独立滚动 → 滚窗口（整页）
                const before = window.scrollY;
                window.scrollTo(0, document.body.scrollHeight);
                window.dispatchEvent(new Event('scroll', {bubbles:true}));
                return window.scrollY !== before;
            })()
        """
        try:
            return bool(self.wb.evaluate(js))
        except WebbridgeError:
            return False

    def load_more_jobs(self, max_scrolls: int = 5, wait_sec: float = 2.5) -> int:
        """通过滚动左侧岗位列表（下拉加载）获取更多岗位。

        返回本次新加载的岗位链接数量；0 表示已到底或加载失败。
        ⭐ v2.5：滚动前确保停留在搜索结果页（web/geek）。若页面停在
        job_detail/ 等详情/失效页，真实滚轮只会滚动详情内容，永远无法
        触发 BOSS 的岗位懒加载——这是之前「只能读取一批、系统卡住」的根因。
        """
        if not self._is_search_page():
            logger.info("  🔙 当前不在搜索结果页，先回到搜索页再下拉加载")
            self.back_to_search()

        before = self._count_job_links()
        for _ in range(max_scrolls):
            scrolled = self._scroll_left_list()
            if not scrolled:
                break
            time.sleep(wait_sec)
            after = self._count_job_links()
            if after > before:
                return after - before
        return max(0, self._count_job_links() - before)

    # ══════════ 岗位详情 ══════════

    def _is_search_page(self) -> bool:
        """判断当前页面是否为搜索结果列表页（web/geek），排除 job_detail 详情页"""
        try:
            url = self.wb.get_page_url()
            return "web/geek" in url and "job_detail" not in url
        except Exception:
            return False

    def open_job_detail(self, url: str) -> bool:
        """直接导航到岗位详情页"""
        logger.info(f"  🔗 打开详情页...")
        self.wb.navigate(url)
        time.sleep(3)
        ok = self._wait_page_ready(timeout=10)
        if not ok:
            return False

        # ⭐ v2.5：检测失效/异常跳转，避免把 job_detail/（无岗位 ID）失效页当岗位处理
        try:
            cur_url = self.wb.get_page_url()
            if "job_detail/" in cur_url and not re.search(r"job_detail/[^/?#]+\.html", cur_url):
                logger.warning("  ⚠️ 岗位详情已失效（URL 无岗位 ID），跳过")
                return False
            text = self.wb.get_page_text()
            for marker in JOB_EXPIRED_MARKERS:
                if marker in text:
                    logger.warning(f"  ⚠️ 岗位已失效（{marker}），跳过")
                    return False
        except Exception:
            pass

        # 提取标题用于日志
        try:
            title = self._extract_title()
            if title:
                logger.info(f"  📄 {title}")
        except Exception:
            pass
        return True

    def _extract_meta(self) -> dict:
        """用 JS 提取岗位详情页结构化信息（标题/薪资/公司/学历/经验/城市/类型）"""
        try:
            meta = self.wb.evaluate(EXTRACT_META_JS) or {}
            return meta if isinstance(meta, dict) else {}
        except Exception:
            return {}

    def _is_tag_line(self, line: str) -> bool:
        """判断文本行是否为标签类信息（经验/学历/城市/福利），而非岗位标题。

        ⭐ 注意：只能「整行匹配」短标签，不能用子串匹配——否则「数据分析实习生」
        这类标题会因包含「实习」二字被误判为标签，导致岗位名称丢失。
        """
        # 经验标签（整行）
        if re.fullmatch(r'(经验不限|不限经验|在校/应届|应届生|无需经验|经验?[1-9]年以内|经验?[1-9]-\d+年|经验?10年以上)', line):
            return True
        # 学历标签（整行）
        if re.fullmatch(r'(学历不限|学历本科|学历硕士|学历博士|学历大专|本科|硕士|博士|大专)', line):
            return True
        # 城市/地区标签：含「·」或「-」的短中文串（如「北京·朝阳区」「杭州-滨江」）
        if len(line) <= 10 and re.fullmatch(r'[一-龥·\-]{2,12}', line) and ("·" in line or "-" in line):
            return True
        # 薪资 / 福利标签
        if re.search(r'\d+[kK]|年终奖|五险一金|周末双休|餐补|带薪年假', line):
            return True
        return False

    def _pick_title_line(self, lines: list, exclude: str = "") -> str:
        """从页面文本行中挑选岗位标题（跳过薪资/学历/经验/城市等标签行）

        exclude：需要跳过的行（如已提取出的公司名称），避免把公司名当标题。
        ⭐ v2.7：公司名与岗位名都是文本首行附近的短串，若不跳过公司名，
        文本兜底会把公司名挑成标题，导致「岗位名称 = 公司名称」。
        """
        for line in lines[:30]:
            line = line.strip()
            if not (5 < len(line) < 80):
                continue
            if "K" in line or line.count(" ") >= 5:
                continue
            if self._is_tag_line(line):
                continue
            if exclude and line == exclude:
                continue
            return line
        return ""

    def _extract_title(self) -> str:
        """提取当前页面的岗位标题"""
        meta = self._extract_meta()
        if meta.get("title"):
            return meta["title"]
        text = self.wb.get_page_text()
        return self._pick_title_line([l.strip() for l in text.split("\n") if l.strip()])

    # ══════════ JD 文本提取 ══════════

    def extract_jd_text(self) -> dict:
        """提取当前详情页的 JD 信息

        ⭐ v2.6：新增 公司/学历要求/工作经验/城市/工作类型 提取，并修复岗位标题
        被误取为「经验1-3年」「本科」等标签行的问题——标题优先用 BOSS 网页语义化
        DOM（h1.name）提取，纯文本启发式仅作兜底。
        """
        jd_info = {
            "title": "", "salary": "", "company": "", "education": "",
            "experience": "", "city": "", "job_type": "",
            "company_size": "", "financing": "", "listed": "", "industry": "",
            "description": "", "requirements": "",
        }

        # ── 结构化提取（JS DOM）──
        meta = self._extract_meta()
        for key in ("title", "salary", "company", "education",
                    "experience", "city", "job_type",
                    "company_size", "financing", "listed", "industry"):
            if meta.get(key) not in (None, ""):
                jd_info[key] = meta[key]

        # ⭐ v2.7 防御：标题与公司名相同 = JS 标题选择器抓到了公司名
        # （公司块先于职位块时，整页 .name 会命中公司名）。清空标题，
        # 交给下方的文本兜底重新挑选（并跳过公司名那一行）。
        if (jd_info["title"] and jd_info["company"]
                and jd_info["title"] == jd_info["company"]):
            jd_info["title"] = ""

        try:
            body_text = self.wb.get_page_text()
            lines = [l.strip() for l in body_text.split("\n") if l.strip()]

            # ── 标题兜底（跳过薪资/学历/经验/城市等标签行，且跳过公司名行）──
            if not jd_info["title"]:
                jd_info["title"] = self._pick_title_line(
                    lines, exclude=jd_info.get("company") or "")

            # ── 薪资兜底 ──
            if not jd_info["salary"]:
                for line in lines[:50]:
                    match = re.search(r'(\d+[kK]\s*[-~到至]\s*\d+[kK]|\d+[kK])', line)
                    if match:
                        jd_info["salary"] = match.group(1)
                        break

            # ── 学历/经验 兜底（JS 未识别时扫描前 40 行）──
            if not jd_info["education"] or not jd_info["experience"]:
                for line in lines[:40]:
                    if not jd_info["education"]:
                        for p in EDUCATION_PATTERNS:
                            if p in line:
                                jd_info["education"] = p
                                break
                    if not jd_info["experience"]:
                        for p in EXPERIENCE_PATTERNS:
                            if p in line:
                                jd_info["experience"] = p.replace("经验", "")
                                break
                    if jd_info["education"] and jd_info["experience"]:
                        break

            # ── 公司兜底：招聘者卡片「示例公司 · 人事招聘专员」（公司名在 · 前缀）──
            # ⭐ v2.8：JS 提取失败时，从页面文本找招聘者卡片。BOSS 详情页把公司名放在
            # 人事职位之前，中间用「·」分隔（页面文本会被拆成多行：示例公司 / · / 人事招聘专员）。
            if not jd_info["company"]:
                for i, line in enumerate(lines[:80]):
                    if line.strip() == "·":
                        if i == 0 or i + 1 >= len(lines):
                            continue
                        prev, nxt = lines[i - 1].strip(), lines[i + 1].strip()
                        if (2 <= len(prev) <= 40
                                and re.search(r'(人事|招聘|经理|专员|顾问|HR|猎头)', nxt)
                                and not re.search(r'[\dKk]', prev)):
                            jd_info["company"] = prev
                            break
                    else:
                        m = re.search(r'^(.{2,40})[·・](人事|招聘|经理|专员|顾问)', line)
                        if m and not re.search(r'[\dKk]', m.group(1)):
                            jd_info["company"] = m.group(1).strip()
                            break

            # ── 公司规模 / 融资上市 / 行业 兜底（JS 未识别时扫全文文本）──
            # ⭐ v3.0：为「符合预期/超预期」岗位提供详细信息（规模、是否上市等）。
            if not jd_info["company_size"]:
                m = re.search(r'(少于15人|15-50人|50-150人|150-500人|500-2000人|2000人以上|\d+-\d+人|\d+人以上)', body_text)
                if m:
                    jd_info["company_size"] = m.group(1)
            if not jd_info["financing"]:
                m = re.search(r'(已上市|未上市|拟上市|不需要融资|未融资|天使轮|Pre-A轮|A\+轮|A轮|B\+轮|B轮|C\+轮|C轮|D轮及以上|D轮|E轮|战略融资)', body_text)
                if m:
                    jd_info["financing"] = m.group(1)
            if not jd_info["listed"] and jd_info["financing"]:
                jd_info["listed"] = True if jd_info["financing"] == "已上市" else False
            if not jd_info["industry"]:
                m = re.search(r'(移动互联网|计算机软件|人工智能|大数据|物联网|信息安全|数据服务|企业服务|云计算|互联网|电子商务|游戏|硬件|半导体|通信|电子|金融|证券|保险|银行|医疗|医药|教育|培训|新能源|汽车|机械制造|机械|制造|物流|广告|传媒|房地产|建筑|餐饮|消费|机器人|自动驾驶)', body_text)
                if m:
                    jd_info["industry"] = m.group(1)

            # ── 提取职位描述 ──
            for marker in JD_DESC_MARKERS:
                pos = body_text.find(marker)
                if pos >= 0:
                    next_pos = len(body_text)
                    for m2 in JD_REQUIRE_MARKERS + JD_DESC_MARKERS:
                        p = body_text.find(m2, pos + len(marker))
                        if p > pos and p < next_pos:
                            next_pos = p
                    jd_info["description"] = body_text[pos:min(next_pos, pos + 800)].strip()
                    break
            else:
                mid = len(body_text) // 3
                jd_info["description"] = body_text[mid:mid + 500].strip()

            # ── 提取任职要求 ──
            for marker in JD_REQUIRE_MARKERS:
                pos = body_text.find(marker)
                if pos >= 0:
                    next_pos = len(body_text)
                    for m2 in JD_DESC_MARKERS + ["查看全部", "立即沟通"]:
                        p = body_text.find(m2, pos + len(marker))
                        if p > pos and p < next_pos:
                            next_pos = p
                    jd_info["requirements"] = body_text[pos:min(next_pos, pos + 800)].strip()
                    break

        except Exception as e:
            logger.warning(f"提取 JD 文本时出错: {e}")

        return jd_info

    def full_jd_text(self, jd_info: dict) -> str:
        """将 JD 信息合并为完整文本"""
        parts = []
        if jd_info.get("title"):
            parts.append(f"【职位】{jd_info['title']}")
        if jd_info.get("salary"):
            parts.append(f"【薪资】{jd_info['salary']}")
        if jd_info.get("company"):
            parts.append(f"【公司】{jd_info['company']}")
        if jd_info.get("city"):
            parts.append(f"【城市】{jd_info['city']}")
        if jd_info.get("education"):
            parts.append(f"【学历要求】{jd_info['education']}")
        if jd_info.get("experience"):
            parts.append(f"【工作经验】{jd_info['experience']}")
        if jd_info.get("job_type"):
            parts.append(f"【工作类型】{jd_info['job_type']}")
        if jd_info.get("description"):
            parts.append(f"【职位描述】\n{jd_info['description']}")
        if jd_info.get("requirements"):
            parts.append(f"【任职要求】\n{jd_info['requirements']}")
        return "\n\n".join(parts)

    # ══════════ 投递操作 ══════════

    def click_apply(self) -> bool:
        """点击详情页的「立即沟通」按钮（用 JS 精确定位到详情页的大按钮）"""
        js = """
            (() => {
                // 找所有包含「立即沟通」的可见元素
                const all = [...document.querySelectorAll('*')]
                    .filter(el => el.textContent.trim() === '立即沟通'
                        && el.offsetHeight > 25
                        && el.offsetWidth > 50
                        && el.checkVisibility());
                if (all.length === 0) return false;
                // 选最大的那个（详情页的按钮通常比列表页的大）
                all.sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight);
                const btn = all[0];
                btn.scrollIntoView({block: 'center', behavior: 'instant'});
                // 用 dispatchEvent 模拟真实点击
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return true;
            })()
        """
        try:
            result = self.wb.evaluate(js)
            if not result:
                # 备用：用 mouse_click
                logger.info("  ⚠️ 用备用方式点击...")
                self.wb.mouse_click("//*[contains(text(), '立即沟通')]")
            time.sleep(3)
            return True
        except Exception as e:
            logger.warning(f"点击「立即沟通」失败: {e}")
            return False

    def send_greeting(self) -> bool:
        """发送自动打招呼消息"""
        # 等聊天窗口出现
        if not self._wait_for_text(CHAT_SEND, timeout=5):
            logger.info("  💬 未检测到聊天窗口，可能无需打招呼")
            return True

        logger.info("  💬 聊天窗口已打开，发送打招呼...")
        time.sleep(1)

        # 点击「发送」按钮
        send_js = """
            (() => {
                const btns = [...document.querySelectorAll('button, a, div, span')]
                    .filter(el => el.textContent.trim() === '发送' && el.offsetHeight > 20);
                if (btns.length === 0) return false;
                // 通常发送按钮在页面底部或弹窗中
                const btn = btns[btns.length - 1];
                btn.scrollIntoView({block: 'center'});
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return true;
            })()
        """
        try:
            self.wb.evaluate(send_js)
            time.sleep(2)
            logger.info("  ✅ 打招呼消息已发送")
            return True
        except Exception as e:
            logger.warning(f"发送打招呼失败: {e}")
            return False

    def close_chat_overlay(self):
        """关闭聊天弹窗，回到岗位详情页"""
        time.sleep(1)
        # 先按 Escape
        try:
            self.wb.send_keys("Escape")
            time.sleep(1)
        except Exception:
            pass
        # 再找关闭按钮
        close_js = """
            (() => {
                const closeBtn = document.querySelector('[class*="close"], [class*="icon-close"], '
                    + '[class*="chat-close"], [class*="modal-close"], '
                    + 'i[class*="close"], .close-btn');
                if (closeBtn) { closeBtn.click(); return true; }
                // 找其他可能的关闭元素
                const all = [...document.querySelectorAll('*')]
                    .filter(el => el.textContent.trim() === '关闭' && el.offsetHeight > 10);
                if (all.length > 0) { all[0].click(); return true; }
                return false;
            })()
        """
        try:
            self.wb.evaluate(close_js)
            time.sleep(1)
        except Exception:
            pass

    def has_applied(self) -> bool:
        """检查当前岗位是否已投递（详情页显示「已沟通」）"""
        try:
            return self.wb.page_has_text(APPLIED_MARKER)
        except Exception:
            return False

    def back_to_search(self):
        """回到搜索结果页（投递后重置状态），确保真的回到搜索页

        ⭐ v2.5：导航后校验是否真的在 web/geek 搜索页；若页面仍停留在
        job_detail/ 等失效/详情页则换 URL 重试，防止引擎卡在详情页导致
        后续滚轮加载不到新岗位。
        """
        for url in [self._last_search_url, SEARCH_URL, f"{BOSS_URL}/web/geek/jobs"]:
            if not url:
                continue
            try:
                self.wb.navigate(url)
                time.sleep(3)
                self._wait_page_ready(timeout=10)
                if self._is_search_page():
                    # 记录当前实际搜索 URL，后续返回使用同样的 URL
                    try:
                        self._last_search_url = self.wb.get_page_url()
                    except Exception:
                        pass
                    return
            except Exception:
                continue
        # 全部候选失败：再兜底导航一次搜索页
        try:
            self.wb.navigate(SEARCH_URL)
            time.sleep(3)
            self._wait_page_ready(timeout=10)
        except Exception:
            pass

    # ══════════ 翻页功能 ══════════

    def has_next_page(self) -> bool:
        """检查是否有下一页"""
        try:
            return self.wb.page_has_text(PAGE_NEXT)
        except Exception:
            return False

    def go_next_page(self) -> bool:
        """翻到下一页"""
        try:
            self.wb.mouse_click(f"//*[contains(text(), '{PAGE_NEXT}')]")
            time.sleep(3)
            self._wait_page_ready(timeout=10)
            logger.info("  📄 已翻到下一页")
            return True
        except Exception as e:
            logger.warning(f"翻页失败: {e}")
            return False

    # ══════════ 状态检测 ══════════

    def has_captcha(self) -> bool:
        """检测是否有验证码"""
        try:
            return self.wb.page_has_text("验证码") or self.wb.page_has_text("安全验证")
        except Exception:
            return False

    def handle_captcha(self):
        """检测到验证码时暂停，让用户手动处理"""
        if self.has_captcha():
            logger.info("🔐 检测到验证码，请在浏览器中手动完成验证...")
            deadline = time.time() + 120
            while time.time() < deadline:
                time.sleep(3)
                if not self.has_captcha():
                    logger.info("✅ 验证码已通过")
                    return
            logger.warning("⚠️ 验证码超时，继续执行")

    def wait_for_page_settle(self, extra_delay: float = 0):
        """等待页面稳定（简化版，兼容旧调用）"""
        time.sleep(2 + extra_delay)
