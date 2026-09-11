# YouTube Karaoke

ฟังเพลงบน YouTube ตามปกติ แล้วให้ AI ตัดเสียงร้องออกแบบสด พร้อมคำร้องไล่สีตามจังหวะ ทำงานบนเครื่องของคุณเอง (ไม่มีค่าใช้จ่าย ไม่ส่งข้อมูลออกไปไหน)

- ตัดเสียงร้องด้วย [Demucs](https://github.com/facebookresearch/demucs) ปรับระดับเสียงร้อง 0-100% ได้ระหว่างเล่น
- คำร้องจาก subtitle ของ YouTube (ถ้ามี) จับจังหวะต่อคำด้วย [Whisper](https://github.com/SYSTRAN/faster-whisper) ถ้าไม่มีก็ให้ Whisper ถอดเอง รองรับภาษาไทยด้วย [Thonburian Whisper](https://github.com/biodatlab/thonburian-whisper)
- ใช้กับหน้า YouTube จริง (login ได้ เล่น playlist ได้) ผ่าน browser extension หรือใช้หน้าเว็บของโปรแกรมเองก็ได้
- เพลงใหม่ 4 นาที: เสียงเริ่มเล่นได้ใน ~7 วินาที คำร้องพร้อมใน ~25 วินาที (RTX 3070) เพลงถัดไปใน playlist เตรียมล่วงหน้าให้

## สิ่งที่ต้องมี

- Windows 10/11 + การ์ดจอ NVIDIA (VRAM 8GB ขึ้นไป) และไดรเวอร์ล่าสุด
- [Python 3.10-3.12](https://www.python.org/downloads/) (ตอนติดตั้งติ๊ก "Add to PATH")
- Chrome หรือ Edge

## ติดตั้ง (ครั้งเดียว)

เปิด PowerShell ในโฟลเดอร์โปรเจกต์แล้วรัน

```powershell
git clone https://github.com/Panudeth/youtube-karaoke.git
cd youtube-karaoke
python -m venv .venv
.venv\Scripts\pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
.venv\Scripts\pip install -r requirements.txt
winget install Gyan.FFmpeg
```

ครั้งแรกที่รัน โปรแกรมจะดาวน์โหลดโมเดล AI เองประมาณ 7GB (Demucs, Whisper large-v3, Whisper ภาษาไทย) รอสักครู่

## ใช้งาน

1. ดับเบิลคลิก `run.bat` เปิด server ค้างไว้ (หน้าต่างดำจะเปิดอยู่ตลอดที่ใช้)
2. ติดตั้ง extension ครั้งเดียว: เปิด `chrome://extensions` เปิด **Developer mode** กด **Load unpacked** เลือกโฟลเดอร์ `extension`
   หน้า "ตั้งค่าการเชื่อมต่อ" จะเปิดขึ้นเอง ถ้า Chrome ถามเรื่องเครือข่ายภายในเครื่อง กด **Allow**
3. เปิด youtube.com เล่นเพลงอะไรก็ได้ คลิกที่หน้าเว็บหนึ่งครั้ง แผง 🎤 Karaoke Live มุมขวาล่างจะเริ่มทำงาน

บนแผง: สวิตช์ **ตัดเสียงร้อง**, slider เสียงร้อง/ดนตรี, สวิตช์ **คำร้อง**, ตัวเลือก **ภาษา** (ถ้าระบบทายผิด), และ **รอให้พร้อมก่อนเล่น** ซึ่งจะหยุดวิดีโอไว้จนเสียงกับคำร้องพร้อม (กดเล่นเพื่อข้ามได้)

ไม่อยากใช้ extension: เปิด http://127.0.0.1:8765 วาง URL เพลงหรือ playlist แล้วกดเล่นได้เลย

## ปรับแต่ง

แก้ค่าที่หัวไฟล์ `run.bat`

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `KARAOKE_MODEL` | `htdemucs` | `htdemucs_ft` สะอาดขึ้นเล็กน้อยแต่ช้ากว่า 4 เท่า |
| `KARAOKE_ACCOMP` | `mix_minus_vocals` | `sum_stems` ร้องหายเกลี้ยงกว่าแต่ดนตรีทึบกว่า |
| `KARAOKE_LYRICS` | `1` | `0` ปิดระบบคำร้อง |
| `KARAOKE_LANG` | (ว่าง = อัตโนมัติ) | `th` บังคับภาษาไทยทุกเพลง |
| `KARAOKE_WHISPER_TH_MODEL` | โมเดลไทย | ตั้งว่างเพื่อปิด ประหยัด VRAM ~1.6GB |

รายละเอียดการทำงาน โครงสร้างไฟล์ และการแก้ปัญหา อยู่ใน [docs/DETAILS.md](docs/DETAILS.md)

## ปัญหาที่พบบ่อย

- **แผงขึ้น "ต่อ server ไม่ได้"** ตรวจว่า `run.bat` ยังเปิดอยู่ แล้วกดปุ่ม "ตั้งค่าการเชื่อมต่อ" บนแผง กด Allow
- **เสียงเงียบ** คลิกที่หน้าเว็บหนึ่งครั้ง (เบราว์เซอร์บังคับให้มีการคลิกก่อนเปิดระบบเสียง)
- **โหลดเพลงไม่ได้** YouTube เปลี่ยนระบบบ่อย อัปเดตด้วย `.venv\Scripts\pip install -U yt-dlp`
- **VRAM ไม่พอ** ตั้ง `KARAOKE_WHISPER_TH_MODEL=` ว่าง หรือ `KARAOKE_WHISPER_MODEL=medium`

## ข้อควรรู้

ใช้ส่วนตัวเท่านั้น โปรแกรมดาวน์โหลดเสียงจาก YouTube มาประมวลผลบนเครื่อง ซึ่งขัดกับข้อกำหนดการใช้งานของ YouTube และไม่เหมาะกับการนำไปเผยแพร่บน Chrome Web Store
