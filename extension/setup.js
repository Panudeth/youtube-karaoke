// Opens from the extension's own origin so Chrome can show the Local Network Access prompt.
// Once granted for this origin, the background service worker (same origin) can reach the server too.
const SERVER = 'http://127.0.0.1:8765';
const result = document.getElementById('result');

async function test() {
  result.className = ''; result.textContent = 'กำลังทดสอบ...';
  try {
    const r = await fetch(SERVER + '/api/status', { cache: 'no-store' });
    const j = await r.json();
    result.className = 'ok';
    result.textContent = `เชื่อมต่อสำเร็จ ✓ server พร้อม (โมเดล ${j.quality || ''}${j.model_ready ? '' : ' กำลังโหลด...'}) ปิดหน้านี้แล้ว refresh แท็บ YouTube ได้เลย`;
    chrome.storage?.local?.set({ lnaGranted: true });
  } catch (e) {
    result.className = 'err';
    result.textContent = `ยังเชื่อมต่อไม่ได้: ${e.message}. ตรวจว่า run.bat เปิดอยู่ แล้วกดปุ่มทดสอบอีกครั้ง หากมีหน้าต่างขออนุญาตให้กด Allow`;
  }
}
document.getElementById('retry').onclick = test;
test();
