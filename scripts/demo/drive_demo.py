"""
Demo recording driver (scripts/record-demo.sh calls this; not meant to be
run standalone in normal use). Drives a real, visible Chromium against
Open WebUI to record the demo GIF (docs/briefs/readme-rework.md, point
2): task start -> URL-fabrication guardrail correcting itself -> approval
requested and granted -> task completes.

Everything here is best-effort against Open WebUI's live DOM, which this
repo does not vendor (pulled image, ghcr.io/open-webui/open-webui) --
selectors are written defensively (role/text/type based, not brittle CSS
classes) but the three points marked "VERIFY LIVE" below are exactly
where a version drift is most likely to require a selector tweak. Run
once with --headed-debug (keeps the window open on failure) to check.

Requires an EXISTING Open WebUI account -- WEBUI_AUTH=true in this
project's docker-compose.yml, so there is no auth-less path. Set
DEMO_OWUI_EMAIL / DEMO_OWUI_PASSWORD (see scripts/record-demo.sh).
"""
import hashlib
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OPEN_WEBUI_URL = os.environ.get("OPEN_WEBUI_URL", "http://localhost:3000")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8090")
OWUI_EMAIL = os.environ["DEMO_OWUI_EMAIL"]
OWUI_PASSWORD = os.environ["DEMO_OWUI_PASSWORD"]
READY_SENTINEL = Path(os.environ.get("DEMO_READY_SENTINEL", "/tmp/demo-ready"))
DONE_SENTINEL = Path(os.environ.get("DEMO_DONE_SENTINEL", "/tmp/demo-done"))

# Same text sent as the first user message -- hashed the same way
# langgraph-agent derives its own thread_id (_derive_thread_id,
# services/langgraph-agent/app/main.py: sha256(first_human_message)[:16]),
# so the visual-capture vignette URL is known before the conversation
# even starts. Must match character for character what gets typed below.
TASK_PROMPT = (
    "Va sur http://fixture-demo-catalog/catalog/index.html et trouve le prix "
    "du produit de référence KX-4471 (il n'est pas sur la première page), "
    "puis mets à jour son stock à 0 sur http://fixture-demo-admin/stock."
)


def thread_id_for(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


VIGNETTE_CSS = """
#demo-vignette-frame {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 340px;
  z-index: 999999;
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  border: 2px solid #4f46e5;
  background: #18181b;
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
#demo-vignette-label {
  color: #fafafa;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding: 6px 10px;
  opacity: 0.85;
}
#demo-vignette-img {
  display: block;
  width: 100%;
  height: auto;
}
"""


def inject_vignette(page, thread_id: str) -> None:
    """Floating inset showing the agent's own live browser (mcp-client's
    CAMPAIGN_VISUAL_CAPTURE, served by the dashboard at
    /api/visual/{thread_id} -- see services/dashboard/app/main.py). This
    is the ONLY piece of the recording under our styling control that
    reflects the agent's real navigation; the image content itself
    (rendered by playwright-mcp, a separate browser process) is not."""
    page.add_style_tag(content=VIGNETTE_CSS)
    page.evaluate(
        """([threadId, dashboardUrl]) => {
            const frame = document.createElement('div');
            frame.id = 'demo-vignette-frame';
            const label = document.createElement('div');
            label.id = 'demo-vignette-label';
            label.textContent = 'agent browser';
            const img = document.createElement('img');
            img.id = 'demo-vignette-img';
            const src = `${dashboardUrl}/api/visual/${threadId}`;
            img.src = src;
            frame.appendChild(label);
            frame.appendChild(img);
            document.body.appendChild(frame);
            setInterval(() => { img.src = src + '?t=' + Date.now(); }, 1000);
        }""",
        [thread_id, DASHBOARD_URL],
    )


def login(page) -> None:
    page.goto(OPEN_WEBUI_URL)
    # VERIFY LIVE: Open WebUI's sign-in form. Written against email/password
    # input types rather than CSS classes to survive minor version drift.
    page.locator('input[type="email"]').fill(OWUI_EMAIL)
    page.locator('input[type="password"]').fill(OWUI_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("networkidle")


def send_task(page) -> None:
    # VERIFY LIVE: Open WebUI's chat input. `[contenteditable="true"]` is
    # the current OWUI chat box implementation; a plain <textarea> is the
    # fallback if that changes.
    box = page.locator('[contenteditable="true"]').first
    if box.count() == 0:
        box = page.locator("textarea").first
    box.click()
    box.type(TASK_PROMPT, delay=15)
    page.keyboard.press("Enter")


def approve_when_prompted(page, timeout_s: int = 120) -> None:
    # VERIFY LIVE: the approval affordance rendered by Open WebUI for a
    # paused TIER_SENSITIVE tool call. Looked for by text rather than a
    # class name -- the langgraph-agent side names this a "function call"
    # confirmation (services/langgraph-agent/app/main.py, /approve).
    approve_button = page.get_by_role("button", name="Approve").or_(
        page.get_by_role("button", name="Continue")
    )
    approve_button.first.wait_for(state="visible", timeout=timeout_s * 1000)
    time.sleep(0.5)  # let the pause render fully before the click shows on camera
    approve_button.first.click()


def wait_for_completion(page, timeout_s: int = 180) -> None:
    page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
    time.sleep(2)  # settle: let the final answer render before we cut


def main() -> None:
    thread_id = thread_id_for(TASK_PROMPT)
    print(f"[drive_demo] thread_id = {thread_id}", file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--window-position=0,0", "--window-size=1280,800"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        login(page)
        inject_vignette(page, thread_id)

        # Signal to record-demo.sh that the window is up and styled --
        # this is the cue to start the ffmpeg capture.
        READY_SENTINEL.touch()
        time.sleep(1)

        send_task(page)
        approve_when_prompted(page)
        wait_for_completion(page)

        DONE_SENTINEL.touch()
        time.sleep(1)
        browser.close()


if __name__ == "__main__":
    main()
