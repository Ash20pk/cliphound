import pytest


@pytest.mark.parametrize("tpl", ["{hook} {", "{oops}", "{hook.__class__}", "{streamer[0]}"])
def test_bad_templates_are_rejected(cliphound, tpl):
    config, _ = cliphound
    with pytest.raises(config.SettingsError):
        config.save_settings({"title_template": tpl})


def test_good_template_saves_and_fills(cliphound):
    config, _ = cliphound
    config.save_settings({"title_template": "{hook} | {streamer} {{literal}}"})
    t = config.load_settings()["title_template"]
    assert config.fill(t, {"hook": "H", "streamer": "S", "credit_url": "u"}) == "H | S {literal}"


def test_fill_never_raises(cliphound):
    config, _ = cliphound
    assert config.fill("{hook} {nope", {"hook": "H", "streamer": "S", "credit_url": "u"}) == "H {nope"


def test_corrupt_settings_fall_back_and_are_kept(cliphound):
    config, _ = cliphound
    config.SETTINGS_FILE.write_text("{not json")
    assert config.load_settings()["clips_per_vod"] == config.DEFAULTS["clips_per_vod"]
    assert list(config.DATA.glob("settings.broken-*.json"))


def test_settings_api_reports_errors(client):
    c, _ = client
    r = c.put("/api/settings", json={"title_template": "{nope}"})
    assert r.status_code == 400 and "{nope}" in r.json()["detail"]
    assert c.put("/api/settings", json={"clips_per_vod": 40}).json()["clips_per_vod"] == 40


def test_safe_defaults(cliphound):
    config, _ = cliphound
    assert config.DEFAULTS["auto_upload"] is False
    assert config.DEFAULTS["privacy"] == "public"
