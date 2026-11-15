"""布局：右侧必须铺满，清理要在「库与任务」里而不是设置页。

这三个都是用户当场指出来的问题：
1. 设置页右边空一大片 —— 因为 form 上有个 max-width:1000px；
2. 清理与重置被塞进设置页，但它其实是「库与任务」的功能；
3. 手机上把表格第二列（目录 / 状态）直接藏掉了。
"""
import re
import unittest
from pathlib import Path

from tests import env  # noqa: F401  (设置 DATA_DIR 等环境变量)

HTML = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"


def html() -> str:
    return HTML.read_text(encoding="utf-8")


class ShellFillsWidthTest(unittest.TestCase):
    def test_shell_is_fixed_sidebar_plus_flexible_main(self):
        css = html()
        self.assertIn(".shell{display:grid;grid-template-columns:248px minmax(0,1fr)", css)

    def test_form_has_no_pixel_max_width(self):
        css = html()
        # max-width:1000px 就是右侧留白 650px 的元凶
        self.assertNotRegex(css, r"form\{[^}]*max-width:\s*\d+px")
        self.assertIn("form{display:grid;grid-template-columns:1fr;gap:12px;max-width:100%;min-width:0}", css)

    def test_settings_cards_stack_vertically(self):
        """设置页一律单列纵向：不许再出现瀑布流/横向多列（用户嫌挤）"""
        css = html()
        self.assertIn("#setCards{display:grid;grid-template-columns:minmax(0,1fr);gap:14px}", css)
        self.assertNotIn("column-width:420px", css)
        self.assertNotIn("#setCards form{display:contents}", css)
        # 卡片内部按宽度自动分列，长字段用 .w 占整行
        self.assertRegex(css, r"fieldset\{[^}]*grid-template-columns:repeat\(auto-fit,minmax\(260px,1fr\)\)")
        self.assertIn(".w{grid-column:1/-1}", css)
        # 媒体与语言：根目录 / 源语言 / 目标语言同一行，2:1:1
        self.assertIn("fieldset.c211{grid-template-columns:minmax(0,2fr) minmax(0,1fr) minmax(0,1fr)}", css)
        self.assertIn('<fieldset class="c211"><div class="fsTitle">媒体与语言</div>', html())
        self.assertNotIn('<label class="w">媒体根目录', html())
        self.assertNotIn("repeat(2,minmax(0,1fr))}\n.fsTitle", css)
        # 渠道配置区同样按宽度自动分列；min(260px,100%) 保证塞进窄档时不撑破卡片
        self.assertIn(".chCfg{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:12px 14px}", css)

    def test_asr_checkboxes_share_one_row(self):
        """三个复选框同一行；那句「关掉会快约 20 倍」的长说明已删"""
        page = html()
        start = page.index('<div class="chkRow">')
        end = page.index("</div>", start)
        block = page[start:end]
        for name in ("asr_word_timestamps", "keep_japanese_srt", "overwrite_existing"):
            self.assertIn(f'name="{name}"', block)
        self.assertNotIn("关掉会快约 20 倍", page)
        self.assertIn(".chkRow{grid-column:1/-1;display:grid;"
                      "grid-template-columns:repeat(auto-fit,minmax(240px,1fr))", html())

    def test_library_columns_are_three_to_one(self):
        """媒体库：目录占 3 份，四个数字列各 1 份"""
        page = html()
        self.assertIn('<col style="width:34px"><col style="width:42%"><col style="width:14%">'
                      '<col style="width:14%"><col style="width:14%"><col style="width:14%">', page)
        self.assertIn("#libView table{min-width:280px;table-layout:fixed}", page)

    def test_translation_channels_and_rhythm_share_one_card(self):
        """渠道顺序和节奏成本合并成一张卡片：渠道在前，节奏在后"""
        page = html()
        start = page.index('<div class="fsTitle">翻译（渠道顺序 + 节奏）</div>')
        end = page.index("</fieldset>", start)
        block = page[start:end]
        self.assertIn('class="chList"', block)
        self.assertIn("节奏与成本", block)
        self.assertLess(block.index('class="chList"'), block.index("节奏与成本"))
        for name in ("translation_batch_size", "daily_char_budget", "tm_enabled"):
            self.assertIn(f'name="{name}"', block)
        # 原来那张独立卡片不许再存在
        self.assertNotIn('<div class="fsTitle">翻译节奏与成本</div>', page)
        self.assertIn(".trRow{grid-column:1/-1;display:grid;"
                      "grid-template-columns:repeat(auto-fit,minmax(220px,1fr))", html())

    def test_jobs_action_column_keeps_buttons_on_one_row(self):
        """操作列给足 120px 固定宽；视频收窄、消息吃掉剩余宽度"""
        page = html()
        self.assertIn('<col style="width:24%"><col style="width:8%"><col style="width:14%"><col><col style="width:120px">', page)
        self.assertIn("th.act,td.act{white-space:nowrap;width:120px}", page)
        self.assertIn("#jobsView table{min-width:620px}", page)
        self.assertIn('<th class="act">操作</th>', page)
        self.assertIn('<td class="act">', page)

    def test_stack_is_one_to_two(self):
        """媒体库 : 任务 = 1 : 2"""
        self.assertIn(".stack{flex:1 1 auto;min-height:0;display:grid;"
                      "grid-template-columns:minmax(0,1fr) minmax(0,2fr)", html())

    def test_account_fields_sit_on_one_row(self):
        """用户名 + 新口令就几个字符，必须并排一行，不许各自拉满整宽占两行"""
        css = html()
        self.assertIn("#accountForm fieldset{grid-template-columns:repeat(2,minmax(0,1fr))", css)
        page = html()
        start = page.index('<form id="accountForm">')
        block = page[start:page.index("</form>", start)]
        self.assertEqual(block.count("<label>"), 2)

    def test_setcards_wrapper_exists(self):
        page = html()
        start = page.index('<div id="setCards">')
        end = page.index("</section>", start)
        block = page[start:end]
        for form_id in ("accountForm", "settings", "aiform"):
            self.assertIn(f'<form id="{form_id}">', block)
            self.assertLess(start, page.index(f'<form id="{form_id}">'))


class CleanupMovedToMainTest(unittest.TestCase):
    """清理属于「库与任务」，两个面板各有一个「清理…」入口，设置页不再有它。"""

    def _view(self, marker: str) -> str:
        page = html()
        start = page.index(marker)
        return page[start:page.index("</section>", start)]

    def test_settings_page_has_no_cleanup_block(self):
        block = self._view('<section id="setView"')
        for gone in ("cleanForm", "清理与重置", "cleanPreview", "cleanRun"):
            self.assertNotIn(gone, block)

    def test_only_library_has_cleanup_button(self):
        """清理入口只留媒体库一个，任务面板不许再放（用户要求删掉）"""
        stack = self._view('<section id="stackView"')
        self.assertIn('id="cleanFromLib"', stack)
        self.assertNotIn('cleanFromJobs', stack)
        self.assertNotIn('>清理…<', stack)
        self.assertIn('>清理</button>', stack)

    def test_cleanup_dialog_has_the_controls(self):
        page = html()
        self.assertIn('<dialog id="cleanDlg">', page)
        for el_id in ("cleanScope", "cleanUseLib", "cleanPreview", "cleanRun",
                      "cleanCancel", "cleanOut", "cleanScopeHint"):
            self.assertIn(f'id="{el_id}"', page)
        self.assertEqual(len(re.findall(r'name="item" value="(?:jobs|index|subtitles|work|audio|tm)"', page)), 6)

    def test_dialog_is_not_a_form_so_stray_enter_cant_submit(self):
        page = html()
        start = page.index('<dialog id="cleanDlg">')
        block = page[start:page.index("</dialog>", start)]
        self.assertNotIn('type="submit"', block)


class ChannelRowTest(unittest.TestCase):
    """三档渠道必须同一行，第三档只留免费的两个，所以要窄一些"""

    def test_channel_slots_share_one_row(self):
        css = html()
        m = re.search(r"\.chList\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,.6fr)", m.group(1))

    def test_channels_stack_on_mobile(self):
        css = html()
        self.assertIn(".chList{grid-template-columns:1fr}", css)

    def test_third_slot_only_offers_free_channels(self):
        page = html()
        start = page.index('class="chPick" data-slot="2"')
        block = page[start:page.index("</select>", start)]
        values = re.findall(r'<option value="([^"]*)"', block)
        self.assertEqual(values, ["", "bing", "google"])

    def test_scrape_test_button_sits_right_after_hourly_limit(self):
        page = html()
        limit = page.index('name="scrape_hourly_limit"')
        bar = page.index('id="scrapeTest"')
        self.assertLess(limit, bar)
        between = page[limit:bar]
        self.assertNotIn('id="scrapeList"', between)

    def test_scrape_test_button_stays_on_the_same_row_as_hourly_limit(self):
        """"测试刮削" 塞进输入框那一格，保证同一行同一高度"""
        page = html()
        self.assertIn('<fieldset class="scrapeGrid">', page)
        self.assertIn("fieldset.scrapeGrid{grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,1.5fr)}", page)
        self.assertIn(".hrsRow{display:flex;gap:8px;align-items:center;min-width:0}", page)
        # 按钮必须写在 scrape_hourly_limit 那个 fieldset 里，且不再单独占一格
        start = page.index('name="scrape_hourly_limit"')
        block = page[start:page.index("</label>", start)]
        self.assertIn('id="scrapeTest"', block)
        self.assertNotIn('class="bar scrapeBar"', page)
        # 手机断点要回单列，否则窄屏塞三个会挤爆
        self.assertIn("fieldset.scrapeGrid,#accountForm fieldset", page)

    def test_scrape_hints_share_one_row(self):
        """流程说明和刮削状态同一行，靠 .scrapeFoot 的 flex 排"""
        page = html()
        self.assertIn('<div class="scrapeFoot">', page)
        self.assertIn(".scrapeFoot{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:4px 20px;align-items:baseline;justify-content:space-between}", page)
        start = page.index('<div class="scrapeFoot">')
        block = page[start:page.index("</fieldset>", start)]
        self.assertIn('id="scrapeList"', block)


class MobileTest(unittest.TestCase):
    def test_table_columns_are_not_hidden_on_mobile(self):
        css = html()
        self.assertNotIn("th:nth-child(2),td:nth-child(2){display:none}", css)
        self.assertIn("table{min-width:560px}", css)

    def test_narrow_screens_use_five_stat_columns(self):
        css = html()
        self.assertIn("@media(max-width:480px)", css)
        self.assertIn("grid-template-columns:repeat(5,minmax(0,1fr))", css)

    def test_empty_state_text_is_not_cut_by_horizontal_scroll(self):
        css = html()
        self.assertIn(".tablewrap:has(td.empty) table{min-width:0}", css)


if __name__ == "__main__":
    unittest.main()
