"""Static security checks for the free Windows/Tailscale deployment."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_tailscale_compose_binds_web_ports_to_loopback_only() -> None:
    content = (ROOT / "docker-compose.tailscale.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8000:8000"' in content
    assert '"127.0.0.1:8081:8081"' in content
    assert "0.0.0.0:8000" not in content
    assert "0.0.0.0:8081" not in content
    assert "3000:3000" not in content


def test_tailscale_setup_uses_private_serve_not_public_funnel() -> None:
    content = (ROOT / "deploy/windows/setup-aie.ps1").read_text(encoding="utf-8")
    assert "tailscale serve --bg --yes --https=443" in content
    assert "tailscale serve --bg --yes --https=8443" in content
    assert "tailscale funnel" not in content.lower()


def test_windows_stop_preserves_volumes_by_default() -> None:
    content = (ROOT / "deploy/windows/stop-aie.ps1").read_text(encoding="utf-8")
    assert "if ($RemoveData)" in content
    assert "down -v" in content
    assert "Type DELETE to continue" in content
    assert "Persistent database and browser profile were preserved." in content


def test_dockerignore_excludes_local_secrets_and_databases() -> None:
    content = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env.*" in content
    assert "*.sqlite3" in content
    assert "data/database/*" in content
    assert ".git" in content


def test_tailscale_environment_template_contains_no_real_secret() -> None:
    content = (ROOT / "deploy/tailscale.env.example").read_text(encoding="utf-8")
    assert "replace-with-a-long-random-token" in content
    assert "AIE_TAILSCALE_HOST=replace-with-device.tailnet-name.ts.net" in content



def test_windows_bootstrap_installs_expected_prerequisites() -> None:
    content = (ROOT / "deploy/windows/bootstrap-host.ps1").read_text(encoding="utf-8")
    assert 'WingetId "Git.Git"' in content
    assert 'WingetId "Docker.DockerDesktop"' in content
    assert 'WingetId "Tailscale.Tailscale"' in content
    assert "Start-Process powershell.exe -Verb RunAs" in content
    assert ".\deploy\windows\setup-aie.ps1" in content


def test_windows_bootstrap_never_embeds_account_secrets() -> None:
    content = (ROOT / "deploy/windows/bootstrap-host.ps1").read_text(encoding="utf-8").lower()
    assert "password=" not in content
    assert "betpawa_pin" not in content
    assert "api_token=" not in content



def test_native_windows_setup_has_no_docker_dependency() -> None:
    content = (ROOT / "deploy/windows/setup-native-aie.ps1").read_text(encoding="utf-8").lower()
    assert "docker" not in content
    assert "tailscale serve --bg --yes --https=443" in content
    assert "run_dashboard.py --host 127.0.0.1" in content


def test_native_bootstrap_installs_python_not_docker() -> None:
    content = (ROOT / "deploy/windows/bootstrap-native-host.ps1").read_text(encoding="utf-8")
    assert 'WingetId "Python.Python.3.12"' in content
    assert 'WingetId "Tailscale.Tailscale"' in content
    assert "Docker.DockerDesktop" not in content
    assert ".\\deploy\\windows\\setup-native-aie.ps1" in content


def test_native_browser_profile_is_ignored() -> None:
    content = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/browser-profile/" in content
