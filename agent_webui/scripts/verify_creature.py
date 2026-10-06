# 用途：控制台形象位的「渲染客观验证」—— 起 vite preview，用 Playwright 打开，
# 直接读 DOM 几何（缩放值、游走位移、状态 class、眼睛位移），并截一张图。
# 判据是数字，不是"我觉得好看"。
#
# 用法：python agent_webui/scripts/verify_creature.py
# 依赖：playwright（项目根 venv）
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FRONT = os.path.join(os.path.dirname(HERE), "frontend")
VITE = os.path.join(FRONT, "node_modules", ".bin", "vite.cmd")
SHOT = os.path.join(HERE, "_creature_check.png")
PORT = 4173
CREATE_NO_WINDOW = 0x08000000

PROBE = """() => {
  const q = (s) => document.querySelector(s);
  const st = q('.ab-stage'), sc = q('.ab-scaler'), cr = q('.creature');
  const mv = q('.mover'), pair = q('.middle-pair'), bu = q('.dialog-bubble');
  if (!st) return { found: false, body: (document.body.innerText || '').slice(0, 200) };
  const r = st.getBoundingClientRect();
  const crr = cr ? cr.getBoundingClientRect() : null;
  return {
    found: true,
    stage: { w: +r.width.toFixed(1), h: +r.height.toFixed(1), x: +r.left.toFixed(1), y: +r.top.toFixed(1) },
    scaler: sc ? sc.style.transform : null,
    creatureClass: cr ? cr.className : null,
    mover: mv ? mv.style.transform : null,
    eyeTx: pair ? pair.style.getPropertyValue('--tx') : null,
    eyeTy: pair ? pair.style.getPropertyValue('--ty') : null,
    bubbleText: bu ? bu.textContent : null,
    bubbleShown: bu ? bu.classList.contains('show') : null,
    bubbleRect: (bu && bu.classList.contains('show'))
      ? (() => { const r = bu.getBoundingClientRect(); return { w: +r.width.toFixed(1), h: +r.height.toFixed(1) }; })()
      : null,
    bladeText: q('.weapon-blade') ? q('.weapon-blade').textContent : null,
    creatureBox: crr ? { w: +crr.width.toFixed(1), h: +crr.height.toFixed(1) } : null,
  };
}"""


def main():
    # 气泡里可能带 emoji（读的是真实中期输出）—— 控制台是 GBK，不设容错会直接崩
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if not os.path.exists(VITE):
        print("vite not found: %s" % VITE)
        return 2
    prev = subprocess.Popen([VITE, "preview", "--port", str(PORT)], cwd=FRONT,
                            creationflags=CREATE_NO_WINDOW,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print("NEED playwright: %r" % (e,))
        prev.kill()
        return 2
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(channel="msedge", headless=True)
            pg = b.new_page(viewport={"width": 1500, "height": 950})
            pg.goto("http://127.0.0.1:%d" % PORT, wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            a = pg.evaluate(PROBE)
            print("--- probe #1 (t=3.0s) ---")
            for k, v in (a or {}).items():
                print("  %-14s %s" % (k, v))
            # 隔 1.2s 再读一次 mover：游走应该已经动了
            pg.wait_for_timeout(1200)
            b2 = pg.evaluate(PROBE)
            print("--- probe #2 (t=4.2s) ---")
            print("  mover        %s" % (b2.get("mover") if b2 else None))
            print("  eyeTx/Ty     %s / %s" % (b2.get("eyeTx"), b2.get("eyeTy")))
            if a and b2 and a.get("mover") != b2.get("mover"):
                print("RESULT: wander ACTIVE (mover transform changed)")
            elif a and a.get("found"):
                print("RESULT: wander IDLE (transform unchanged)")
            else:
                print("RESULT: STAGE NOT FOUND")
            # 点一下形象，看气泡
            try:
                box = pg.query_selector(".creature")
                if box:
                    box.click(position={"x": 30, "y": 40})
                    pg.wait_for_timeout(500)
                    c = pg.evaluate(PROBE)
                    print("--- after click ---")
                    print("  bubbleShown  %s" % (c.get("bubbleShown") if c else None))
                    print("  bubbleText   %r" % (c.get("bubbleText") if c else None))
                    print("  bubbleRect   %s  (视觉尺寸；字高应 ~18px = 13px x 1.4)" % (c.get("bubbleRect") if c else None))
            except Exception as e:
                print("click failed: %r" % (e,))
            pg.screenshot(path=SHOT)
            print("shot -> %s" % SHOT)
            b.close()
    finally:
        prev.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
