"""设置页：整页只有一个保存入口。

三件事锁住：
1. 表单里出现的每个 name 都必须能被 /api/settings 接收 —— 之前「源语言 / 目标语言」
   两个输入框根本没有对应字段，改了保存也不生效，页面上又看不出来；
2. 「重置」用的默认值来自 /api/settings/defaults，且密钥一律掩码，不能把 key 送出去；
3. 页面上只能有一个「保存」入口，别再加回局部保存按钮。
"""
import re
import unittest
from pathlib import Path

from tests import env  # noqa: F401  (设置 DATA_DIR 等环境变量)

from app import main as app_main
from app.config import Settings
from app.db import init_db, load_settings, save_settings

HTML = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"


def settings_form_names() -> list[str]:
    html = HTML.read_text(encoding="utf-8")
    start = html.index('<form id="settings">')
    end = html.index("</form>", start)
    block = html[start:end]
    return sorted(set(re.findall(r'\bname="([a-z_0-9]+)"', block)))


class FormFieldsAcceptedTest(unittest.TestCase):
    def test_every_settings_field_is_patchable(self):
        fields = set(app_main.SettingsPatch.model_fields)
        missing = [n for n in settings_form_names() if n not in fields]
        self.assertEqual(missing, [], f"设置页这些输入框保存时会被静默丢弃：{missing}")

    def test_language_fields_persist(self):
        init_db()
        save_settings(Settings())
        patch = app_main.SettingsPatch(source_language="ja", target_language="zh-TW")
        app_main.set_settings(patch)
        now = load_settings()
        self.assertEqual((now.source_language, now.target_language), ("ja", "zh-TW"))


class DefaultsTest(unittest.TestCase):
    def test_defaults_come_from_dataclass(self):
        init_db()
        d = app_main.get_settings_defaults()
        fresh = Settings()
        self.assertEqual(d["whisper_model"], fresh.whisper_model)
        self.assertEqual(d["media_root"], fresh.media_root)
        self.assertEqual(d["beam_size"], fresh.beam_size)
        self.assertTrue(d["asr_word_timestamps"])

    def test_defaults_mask_secrets(self):
        d = app_main.get_settings_defaults()
        for k in ("openai_api_key", "google_api_key",
                  "subdl_api_key", "opensubtitles_api_key", "api_token"):
            self.assertEqual(d.get(k), "********" if d.get(k) else d.get(k))

    def test_deepl_key_is_shown_in_plain_text(self):
        """DeepL key 要明文回显 + 输入框不是密码框，方便核对 :fx 有没有抄全"""
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('name="deepl_api_key" type="text"', html)
        self.assertNotIn('name="deepl_api_key" type="password"', html)
        init_db()
        save_settings(Settings(deepl_api_key="abc-123:fx"))
        self.assertEqual(app_main.get_settings()["deepl_api_key"], "abc-123:fx")


class SingleSaveEntryTest(unittest.TestCase):
    def test_only_one_save_button_on_settings_page(self):
        html = HTML.read_text(encoding="utf-8")
        start = html.index('<section id="setView"')
        end = html.index("</section>", start)
        block = html[start:end]
        saves = re.findall(r">\s*保存\s*</button>", block)
        self.assertEqual(len(saves), 1, "设置页只应有标题右侧那一个保存按钮")

    def test_no_submit_button_left_on_settings_page(self):
        html = HTML.read_text(encoding="utf-8")
        start = html.index('<section id="setView"')
        end = html.index("</section>", start)
        self.assertNotIn('type="submit"', html[start:end])

    def test_header_has_three_actions(self):
        html = HTML.read_text(encoding="utf-8")
        for btn in ("setSave", "setCancel", "setReset"):
            self.assertIn(f'id="{btn}"', html)

    def test_no_readonly_runtime_block(self):
        # 「运行节奏（只读）」那块跟状态栏、上面各分组的信息重复，已删掉，不许再加回来
        html = HTML.read_text(encoding="utf-8")
        self.assertNotIn("运行节奏", html)
        self.assertNotIn('id="perfGrid"', html)
        self.assertNotIn("loadPerf", html)

    def test_every_element_id_the_js_uses_exists(self):
        # 删块的时候最容易留下 $('xxx') 指向已不存在的元素（loadPerf 就是这种残留）
        html = HTML.read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))
        used = (set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", html))
                | set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", html))
                | set(re.findall(r"querySelector\('#([A-Za-z0-9_-]+)'\)", html)))
        self.assertEqual(sorted(used - ids), [], "JS 引用了不存在的元素 id")


class AiAccessLayoutTest(unittest.TestCase):
    """AI 接入要一行：左边地址、右边 token（token 列更宽，两个按钮跟在后面）。"""

    def _block(self) -> str:
        html = HTML.read_text(encoding="utf-8")
        start = html.index('<form id="aiform">')
        return html[start:html.index("</form>", start)]

    def test_address_and_token_share_one_row(self):
        block = self._block()
        # 两个字段都必须是 fieldset 的直接子项（不再各占一整行）
        self.assertNotIn('style="grid-column:1/-1">对外访问地址', block)
        self.assertIn("<label>对外访问地址", block)
        self.assertIn("<label>接入 token", block)

    def test_fieldset_gives_token_column_more_room(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('<fieldset class="aiGrid">', html)
        self.assertIn(".aiGrid{grid-template-columns:minmax(0,1fr) minmax(0,1.35fr)}", html)
        # 窄屏必须回退成单列，否则两列挤在手机上一行放不下
        self.assertIn(".aiGrid{grid-template-columns:1fr}", html)

    def test_token_input_can_shrink_and_buttons_never_wrap(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn(".tokenbox input{flex:1 1 0;min-width:0;", html)
        self.assertIn(".tokenbox .mini{flex:none;white-space:nowrap}", html)

    def test_readonly_toggle_and_hint_are_gone(self):
        """AI 接入只留地址 + token 两栏，只读开关和说明整段删掉（用户要求）"""
        block = self._block()
        self.assertNotIn('api_token_readonly', block)
        self.assertNotIn("只能查，不能暂停", block)
        self.assertNotIn("Authorization: Bearer", block)
        self.assertNotIn('style="grid-column:1/-1"', block)
        self.assertIn('name="web_public_url"', block)
        self.assertIn('id="tokenValue"', block)


class AccountRowTest(unittest.TestCase):
    """账号区只要「用户名 + 新口令」两栏，一行两格。"""

    def _block(self) -> str:
        html = HTML.read_text(encoding="utf-8")
        start = html.index('<form id="accountForm">')
        return html[start:html.index("</form>", start)]

    def test_only_two_fields(self):
        names = re.findall(r'\bname="([a-z_0-9]+)"', self._block())
        self.assertEqual(names, ["username", "password"], "账号区只应有用户名和新口令")

    def test_no_current_password_or_confirm(self):
        block = self._block()
        self.assertNotIn("current_password", block)
        self.assertNotIn("password2", block)
        self.assertNotIn("再输一次", block)
        self.assertNotIn("当前口令", block)


if __name__ == "__main__":
    unittest.main()


class MaskWriteBackTest(unittest.TestCase):
    """页面把 "********" 原样存回库里 → 之后真请求 403。载入时必须当空。"""

    def test_masked_key_is_not_persisted(self):
        from app.config import Settings
        s = Settings.from_dict({"deepl_api_key": "********",
                                "openai_api_key": "********",
                                "google_api_key": ""})
        self.assertEqual(s.deepl_api_key, "")
        self.assertEqual(s.openai_api_key, "")
        self.assertEqual(s.google_api_key, "")

    def test_real_key_survives_round_trip(self):
        from app.config import Settings
        key = "00000000-0000-4000-8000-000000000000:fx"
        self.assertEqual(Settings.from_dict({"deepl_api_key": key}).deepl_api_key, key)

    def test_deepl_key_is_plain_in_safe_dict(self):
        from app.config import Settings
        d = Settings(deepl_api_key="abc:fx", openai_api_key="sk-1").safe_dict()
        self.assertEqual(d["deepl_api_key"], "abc:fx")
        self.assertEqual(d["openai_api_key"], "********")
