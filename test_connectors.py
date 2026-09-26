import json
import os
import sys

import pytest

from duopilot import github, providers


def request(stage="discuss", actor="A", topic="cspilot", turn=1):
    return {
        "actor": actor, "topic": topic, "stage": stage, "turn": turn,
        "context": "Exemple", "history": [], "proposal": None, "plan_id": "abc123",
    }


def test_demo_proposal_and_exact_vote_for_both_topics():
    provider = providers.build_provider({}, "demo")
    for topic in ("cspilot", "self"):
        replies = [provider.generate(request(topic=topic, actor="A" if index % 2 else "B", turn=index)) for index in range(1, 5)]
        proposal = provider.generate(request("propose", topic=topic, turn=5))
        vote = provider.generate(request("vote", "B", topic, 6))
        assert len(replies) == 4
        assert proposal.payload["plan"]["acceptance_criteria"]
        assert vote.payload["vote"] == "accept"
        assert vote.payload["plan_id"] == "abc123"
        assert "DÉMONSTRATION" in github.build_context({}, topic, "demo")


@pytest.mark.parametrize("raw", ['{"message":"a","message":"b"}', '{"value":NaN}', '```json\n{}\n```', '[]'])
def test_strict_json_rejects_ambiguous_responses(raw):
    with pytest.raises(providers.ProviderError):
        providers._parse_json(raw)


def test_provider_rejects_malformed_vote():
    with pytest.raises(providers.ProviderError):
        providers._validate_payload({"message": "test", "vote": {}, "plan_id": "abc"}, "vote")


def test_cli_environment_excludes_credentials_and_injected_runtime(monkeypatch):
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY", "ANTHROPIC_BASE_URL", "NODE_OPTIONS", "PYTHONPATH"):
        monkeypatch.setenv(key, "SECRET_VALUE")
    result = providers._clean_env()
    assert "SECRET_VALUE" not in result.values()
    assert result.get("HOME") == os.environ.get("HOME")


def test_cli_process_uses_stdin_and_no_secret_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "SHOULD_NOT_LEAK")
    script = "import os,sys,json; print(json.dumps({'message':sys.stdin.read(),'secret':os.environ.get('GITHUB_TOKEN')}))"
    raw = providers._run_cli([sys.executable, "-c", script], "bonjour", tmp_path, 10)
    assert json.loads(raw) == {"message": "bonjour", "secret": None}


def test_cli_timeout_and_failure_do_not_expose_stderr(tmp_path):
    with pytest.raises(providers.ProviderError, match="Délai"):
        providers._run_cli([sys.executable, "-c", "import time; time.sleep(10)"], "", tmp_path, 0.1)
    with pytest.raises(providers.ProviderError) as failure:
        providers._run_cli([sys.executable, "-c", "import sys; sys.stderr.write('SECRET'); sys.exit(1)"], "", tmp_path, 10)
    assert "SECRET" not in str(failure.value)


def test_subscription_cli_command_contract(monkeypatch):
    monkeypatch.setattr(providers, "validate_live_config", lambda config: None)
    monkeypatch.setattr(providers, "_check_codex_config", lambda: None)
    monkeypatch.setattr(providers.shutil, "which", lambda binary: binary)
    commands = []

    def run(args, prompt, cwd, timeout, output_file=None):
        commands.append(args)
        result = json.dumps({"message": "Proposition de discussion"})
        if args[0] == "claude":
            return json.dumps({"is_error": False, "subtype": "success", "result": result, "usage": {"input_tokens": 12, "output_tokens": 4}})
        return result

    monkeypatch.setattr(providers, "_run_cli", run)
    provider = providers.build_provider({"USE_SUBSCRIPTION_CLI": True}, "live")
    assert provider.generate(request(actor="A")).input_tokens is None
    assert provider.generate(request(actor="B")).input_tokens == 12
    codex, claude = commands
    assert codex[codex.index("--sandbox") + 1] == "read-only"
    assert 'forced_login_method="chatgpt"' in codex
    assert "features.shell_tool=false" in codex
    assert "features.apps=false" in codex
    assert "features.multi_agent=false" in codex
    assert "features.hooks=false" in codex
    assert claude[claude.index("--tools") + 1] == ""
    assert claude[claude.index("--max-turns") + 1] == "1"
    assert "--bare" not in claude
    settings = json.loads(claude[claude.index("--settings") + 1])
    assert settings["disableAllHooks"] is True
    assert settings["forceLoginMethod"] == "claudeai"


def test_github_missing_configuration_fails_instead_of_using_demo():
    with pytest.raises(github.GitHubError, match="CSPILOT_REPO"):
        github.build_context({}, "cspilot", "live")


def test_github_context_preserves_cap_and_bounds(monkeypatch):
    calls = []

    def get(repo, endpoint, params, token):
        calls.append((endpoint, params))
        if endpoint == "issues":
            return [{"number": n, "title": "a" * 300, "body": "b" * 3000, "labels": []} for n in range(20)], True
        if endpoint == "pulls":
            return [{"number": n, "title": "PR", "base": {"ref": "dev"}} for n in range(4)], True
        return [{"sha": "a" * 40, "commit": {"message": "test"}}], False

    monkeypatch.setattr(github, "_get_json", get)
    result = github.build_context({"CSPILOT_REPO": "owner/repo", "MAX_CONTEXT_CHARS": 4000}, "cspilot", "live")
    evidence = json.loads(result.split("\n", 1)[1])
    assert len(result) <= 4000
    assert evidence["new_work_blocked"] is True
    assert evidence["open_pull_requests_observed"] == 4
    assert evidence["omitted_for_context_limit"]["issues"] > 0
    assert calls[-1] == ("commits", {"sha": "dev", "per_page": 5})


def test_github_refuses_redirect_and_does_not_surface_secret(monkeypatch):
    observed = {}

    class Response:
        status_code = 302
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def get(url, **kwargs):
        observed.update(kwargs)
        assert url.startswith("https://api.github.com/repos/")
        return Response()

    monkeypatch.setattr(github.requests, "get", get)
    with pytest.raises(github.GitHubError) as failure:
        github._get_json("owner/repo", "issues", {}, "MY_SECRET")
    assert observed["allow_redirects"] is False
    assert "MY_SECRET" not in str(failure.value)
