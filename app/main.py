import asyncio
import os
import socket
import time
import urllib.request
import http.client
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse

load_dotenv()

CAM_IP = os.getenv("CAM_IP", "192.168.54.1")
UDP_PORT = int(os.getenv("UDP_PORT", 49152))
DEVICE_NAME = os.getenv("DEVICE_NAME", "MyPC")
LUMIX_UUID = "4D454930-0100-1000-8001-020B00000000"
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Lumix DMC-TZ100 API")


def cam_cgi(params: str, timeout: int = 5) -> str:
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
    acc = cam_cgi(f"mode=accctrl&type=req_acc&value={LUMIX_UUID}&value2={DEVICE_NAME}", timeout=10)
    rec = cam_cgi("mode=camcmd&value=recmode", timeout=10)
    time.sleep(1.0)
    stream = cam_cgi(f"mode=startstream&value={UDP_PORT}")
    state_xml = cam_cgi("mode=getstate")
    batt = ""
    if "<batt>" in state_xml:
        batt = ET.fromstring(state_xml).findtext(".//batt", "")
    return {"acc": acc, "rec": rec, "stream": stream, "batt": batt}


@app.post("/api/cmd/{command}")
def send_command(command: str):
    res = cam_cgi(f"mode=camcmd&value={command}")
    if command == "recmode":
        time.sleep(1.0)
        cam_cgi(f"mode=startstream&value={UDP_PORT}")
    return {"result": res}


@app.post("/api/setting")
def set_setting(type: str = Query(...), value: str = Query(...), value2: str = Query(None)):
    q = f"mode=setsetting&type={type}&value={value}" + (f"&value2={value2}" if value2 else "")
    return {"result": cam_cgi(q)}


@app.get("/api/menu")
def get_dynamic_menu():
    xml_path = BASE_DIR.parent / "tz100_allmenu.xml"
    if not xml_path.exists():
        return {"groups": []}

    root = ET.fromstring(xml_path.read_text(encoding="utf-8"))
    titles = {
        t.attrib["id"]: t.text
        for lang in ("en", "ru")
        for t in root.findall(f".//titlelist/language[@code='{lang}']/title")
        if t.attrib.get("id") and t.text
    }

    settings_map = {}
    for item in root.findall(".//item[@cmd_mode='setsetting']"):
        if item.find("group") is not None:
            continue
        ctype, cval, cval2 = item.get("cmd_type"), item.get("cmd_value"), item.get("cmd_value2")
        if not ctype or not cval or "__value__" in (cval, cval2):
            continue

        full_val = f"{cval}|{cval2}" if cval2 else cval
        label = titles.get(item.get("title_id", ""), cval2 or cval)
        opts = settings_map.setdefault(ctype, [])
        if not any(o["value"] == full_val for o in opts):
            opts.append({"value": full_val, "label": label})

    wanted = [
        ("quality", "title_quality"),
        ("iso", "title_sensitivity"),
        ("exposure", "title_exposure2"),
        ("whitebalance", "title_whitebalance"),
        ("videoquality", "title_v_quality"),
        ("drivemode", "title_drivemode"),
        ("afmode", "title_afmode"),
        ("focusmode", "title_focusmode"),
    ]
    return {
        "groups": [
            {"type": k, "title": titles.get(tid, k), "options": settings_map[k]}
            for k, tid in wanted if k in settings_map
        ]
    }


async def mjpeg_generator():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    sock.settimeout(0.2)
    loop = asyncio.get_running_loop()
    last_ka = time.time()

    try:
        while True:
            if time.time() - last_ka > 8:
                loop.run_in_executor(None, cam_cgi, "mode=getstate")
                last_ka = time.time()
            try:
                data, _ = await loop.run_in_executor(None, sock.recvfrom, 65535)
                soi = data.find(b"\xff\xd8")
                eoi = data.rfind(b"\xff\xd9")
                if soi != -1 and eoi != -1:
                    jpeg = data[soi:eoi + 2]
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            except socket.timeout:
                await asyncio.sleep(0.01)
    finally:
        sock.close()


@app.get("/api/stream")
async def video_stream():
    return StreamingResponse(mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame")


def browse_dlna(start: int, count: int):
    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
 <s:Body>
  <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
   <ObjectID>0</ObjectID>
   <BrowseFlag>BrowseDirectChildren</BrowseFlag>
   <Filter>*</Filter>
   <StartingIndex>{start}</StartingIndex>
   <RequestedCount>{count}</RequestedCount>
   <SortCriteria></SortCriteria>
  </u:Browse>
 </s:Body>
</s:Envelope>"""
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'
    }
    r = requests.post(f"http://{CAM_IP}:60606/Server0/CDS_control", data=soap, headers=headers, timeout=6)
    root = ET.fromstring(r.text)
    total = int(root.findtext(".//TotalMatches") or 0)
    num_ret = int(root.findtext(".//NumberReturned") or 0)
    didl = root.findtext(".//Result")
    return total, num_ret, (ET.fromstring(didl) if didl else None)


@app.get("/api/files")
def list_files(offset: int = 0, limit: int = 45):
    if offset == 0:
        cam_cgi("mode=camcmd&value=playmode")
        time.sleep(0.5)

    try:
        total, _, _ = browse_dlna(0, 1)
        if total == 0 or offset >= total:
            return {"total": total, "items": [], "has_more": False}

        end_idx = total - offset
        start_idx = max(0, end_idx - limit)
        ns = {"didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/", "dc": "http://purl.org/dc/elements/1.1/"}
        items = []

        curr = start_idx
        while curr < end_idx:
            req_count = min(15, end_idx - curr)
            _, num_ret, didl_root = browse_dlna(curr, req_count)
            if not didl_root or num_ret == 0:
                break

            for item in didl_root.findall("didl:item", ns):
                title = item.findtext("dc:title", "", ns)
                res_list = item.findall("didl:res", ns)

                dt_url = None
                ds_url = None
                ds_size = 0
                do_url = None
                do_size = 0
                do_ext = "JPG"

                for res in res_list:
                    url = res.text
                    if not url:
                        continue
                    size = int(res.get("size") or 0)
                    proto = res.get("protocolInfo") or ""
                    fname = url.split("/")[-1].upper()

                    if fname.startswith("DT"):
                        dt_url = url
                    elif fname.startswith("DS"):
                        ds_url = url
                        ds_size = size
                    elif fname.startswith("DO"):
                        do_url = url
                        do_size = size
                        # Если это RAW (метка UNKNOWN_DATA или CAM_RAW), принудительно меняем расширение на RW2
                        if "UNKNOWN_DATA" in proto or "CAM_RAW" in proto or size == 0:
                            do_ext = "RW2"
                            # Исправляем URL с .JPG на .RW2, чтобы камера отдала оригинал
                            if do_url.upper().endswith(".JPG"):
                                do_url = do_url[:-4] + ".RW2"
                        else:
                            do_ext = fname.split(".")[-1]

                files = []
                if do_url:
                    mb = round(do_size / (1024 * 1024), 1) if do_size > 0 else 22.2  # Примерный размер RAW если size=0
                    label = f"{do_ext} ({mb} МБ)" if mb >= 1 else do_ext
                    files.append({"url": do_url, "ext": do_ext, "label": label, "size": do_size})

                items.append({
                    "title": title,
                    "thumb": dt_url or ds_url,
                    "files": files
                })

            curr += num_ret

        items.reverse()
        return {"total": total, "items": items, "has_more": start_idx > 0}
    except Exception as e:
        return {"error": str(e), "items": [], "has_more": False}


@app.get("/api/download")
def download_file(url: str = Query(...), filename: str = Query(...), size: int = Query(0)):
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 50001, timeout=30)
    conn.request("GET", parsed.path, headers={
        "User-Agent": "Panasonic Android/1.0 UPnP/1.0 DLNA/1.5",
        "Connection": "close"
    })
    resp = conn.getresponse()
    is_chunked = resp.getheader("Transfer-Encoding", "").lower() == "chunked"

    def clean_stream():
        try:
            if is_chunked:
                while True:
                    line = resp.fp.readline()
                    if not line:
                        break
                    line_str = line.strip().split(b";")[0]
                    if not line_str:
                        continue
                    try:
                        chunk_len = int(line_str, 16)
                    except ValueError:
                        break
                    if chunk_len == 0:
                        break
                    data = resp.fp.read(chunk_len)
                    resp.fp.read(2)
                    if data:
                        yield data
                    if len(data) < chunk_len:
                        break
            else:
                while chunk := resp.fp.read(262144):
                    yield chunk
        except (TimeoutError, socket.timeout):
            pass
        finally:
            conn.close()

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if size > 0 and not is_chunked:
        headers["Content-Length"] = str(size)

    return StreamingResponse(clean_stream(), media_type="application/octet-stream", headers=headers)