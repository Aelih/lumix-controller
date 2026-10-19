import asyncio
import os
import socket
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
import urllib.request

load_dotenv()

CAM_IP = os.getenv("CAM_IP", "192.168.54.1")
UDP_PORT = int(os.getenv("UDP_PORT", 49152))
DEVICE_NAME = os.getenv("DEVICE_NAME", "FastAPI_PC")
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Lumix DMC-TZ100 API")


def cam_cgi(params: str, timeout: int = 8) -> str:
    try:
        r = requests.get(f"http://{CAM_IP}/cam.cgi?{params}", timeout=timeout)
        return r.text
    except Exception as e:
        return f"error: {e}"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.post("/api/connect")
def connect_camera():
    # Даем камере до 15 секунд на случай, если нужно подтвердить подключение на экране
    res_acc = cam_cgi("mode=accctrl&type=req_acc&value=0&value2=MyPC", timeout=15)
    time.sleep(0.5)
    res_rec = cam_cgi("mode=camcmd&value=recmode", timeout=10)
    time.sleep(1.5)  # Ждем механику объектива
    res_str = cam_cgi(f"mode=startstream&value={UDP_PORT}", timeout=5)
    print(f"[CONNECT] acc={res_acc.strip()} | rec={res_rec.strip()} | stream={res_str.strip()}")
    return {"acc": res_acc, "rec": res_rec, "stream": res_str}


@app.post("/api/cmd/{command}")
def send_command(command: str):
    if command == "recmode":
        res = cam_cgi("mode=camcmd&value=recmode")
        time.sleep(1.2)
        cam_cgi(f"mode=startstream&value={UDP_PORT}")
        return {"result": res}
    return {"result": cam_cgi(f"mode=camcmd&value={command}")}


@app.post("/api/setting")
def set_setting(type: str = Query(...), value: str = Query(...)):
    return {"result": cam_cgi(f"mode=setsetting&type={type}&value={value}")}


async def mjpeg_generator():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    sock.settimeout(0.2)

    loop = asyncio.get_running_loop()
    # Ставим текущий time.time(), чтобы НЕ бомбить камеру запросами в первую же секунду
    last_keepalive = time.time()
    frames_received = 0

    try:
        while True:
            if time.time() - last_keepalive > 8:
                loop.run_in_executor(None, cam_cgi, "mode=getstate")
                last_keepalive = time.time()

            try:
                data, _ = await loop.run_in_executor(None, sock.recvfrom, 65535)
                soi = data.find(b"\xff\xd8")
                eoi = data.rfind(b"\xff\xd9")
                if soi != -1 and eoi != -1:
                    jpeg = data[soi:eoi + 2]
                    frames_received += 1
                    if frames_received == 1:
                        print(f"[STREAM] Пошел видеопоток! Первый кадр: {len(jpeg)} байт")
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" +
                        jpeg + b"\r\n"
                    )
            except socket.timeout:
                await asyncio.sleep(0.01)
    finally:
        sock.close()


@app.get("/api/stream")
async def video_stream():
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/files")
def list_files():
    cam_cgi("mode=camcmd&value=playmode")
    time.sleep(0.6)

    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'
    }
    ns = {
        "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
        "dc": "http://purl.org/dc/elements/1.1/"
    }

    # Группируем по title (например, "118-0746"), чтобы объединить RAW+JPG в одну карточку
    grouped = {}
    start = 0
    batch_size = 50
    total = None

    try:
        while True:
            soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
 <s:Body>
  <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
   <ObjectID>0</ObjectID>
   <BrowseFlag>BrowseDirectChildren</BrowseFlag>
   <Filter>*</Filter>
   <StartingIndex>{start}</StartingIndex>
   <RequestedCount>{batch_size}</RequestedCount>
   <SortCriteria></SortCriteria>
  </u:Browse>
 </s:Body>
</s:Envelope>"""

            r = requests.post(
                f"http://{CAM_IP}:60606/Server0/CDS_control",
                data=soap_body,
                headers=headers,
                timeout=5
            )
            root = ET.fromstring(r.text)
            didl_xml = root.find(".//Result").text
            number_returned = int(root.find(".//NumberReturned").text or 0)

            if total is None:
                total = int(root.find(".//TotalMatches").text or 0)

            if not didl_xml or number_returned == 0:
                break

            didl_root = ET.fromstring(didl_xml)

            for item in didl_root.findall("didl:item", ns):
                title = item.find("dc:title", ns).text
                res_list = item.findall("didl:res", ns)

                if title not in grouped:
                    grouped[title] = {"title": title, "thumb": None, "files_map": {}}

                entry = grouped[title]

                for res in res_list:
                    url = res.text
                    if not url:
                        continue

                    filename = url.split("/")[-1].upper()
                    ext = filename.split(".")[-1]
                    proto = res.attrib.get("protocolInfo", "")

                    # У Panasonic превью начинаются с DT или DL, а оригиналы — с DO
                    is_thumb = (
                        filename.startswith("DT")
                        or filename.startswith("DL")
                        or "JPEG_TN" in proto
                        or "JPEG_SM" in proto
                        or "CAM_TN" in proto
                        or "CAM_LRGTN" in proto
                    )

                    if is_thumb:
                        # Для превью в галерее предпочитаем DT (быстрее грузится) или DL
                        if entry["thumb"] is None or filename.startswith("DT"):
                            entry["thumb"] = url
                    else:
                        # Это оригинал (JPG, RW2 или MP4) — сохраняем ровно 1 экземпляр каждого формата
                        if ext not in entry["files_map"]:
                            entry["files_map"][ext] = {"url": url, "ext": ext}

            start += number_returned
            if start >= total:
                break

        # Формируем итоговый список
        items = []
        for title, data in grouped.items():
            files = list(data["files_map"].values())
            if not files and data["thumb"]:
                files = [{"url": data["thumb"], "ext": "JPG"}]

            items.append({
                "title": title,
                "thumb": data["thumb"] or (files[0]["url"] if files else ""),
                "files": files
            })

        # Свежие кадры — в начало списка
        items.reverse()
        return {"total": len(items), "items": items}

    except Exception as e:
        return {"error": str(e), "items": []}

@app.get("/api/download")
def download_file(url: str = Query(...), filename: str = Query(...)):
    """Скачивает сырой поток с камеры в обход бага кривого Transfer-Encoding: chunked."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "LumixController/1.0",
        "Connection": "close"
    })
    # Открываем соединение (таймаут 30 секунд на случай больших 4K файлов)
    resp = urllib.request.urlopen(req, timeout=30)

    # ГЛАВНЫЙ ФИКС: запрещаем Python искать chunk-заголовки в сыром потоке камеры
    resp.chunked = False

    def file_stream():
        try:
            while True:
                chunk = resp.read(262144)  # Читаем блоками по 256 КБ
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    if resp.headers.get("Content-Length"):
        headers["Content-Length"] = resp.headers.get("Content-Length")

    return StreamingResponse(
        file_stream(),
        media_type="application/octet-stream",
        headers=headers
    )