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

load_dotenv()

CAM_IP = os.getenv("CAM_IP", "192.168.54.1")
UDP_PORT = int(os.getenv("UDP_PORT", 49152))
DEVICE_NAME = os.getenv("DEVICE_NAME", "FastAPI_PC")
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Lumix DMC-TZ100 API")


def cam_cgi(params: str) -> str:
    try:
        r = requests.get(f"http://{CAM_IP}/cam.cgi?{params}", timeout=3)
        return r.text
    except Exception as e:
        return f"error: {e}"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.post("/api/connect")
def connect_camera():
    res_acc = cam_cgi(f"mode=accctrl&type=req_acc&value=0&value2={DEVICE_NAME}")
    res_rec = cam_cgi("mode=camcmd&value=recmode")
    res_str = cam_cgi(f"mode=startstream&value={UDP_PORT}")
    return {"acc": res_acc, "rec": res_rec, "stream": res_str}


@app.post("/api/cmd/{command}")
def send_command(command: str):
    if command == "recmode":
        res = cam_cgi("mode=camcmd&value=recmode")
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
    sock.setblocking(False)

    loop = asyncio.get_running_loop()
    last_keepalive = time.time()

    try:
        while True:
            if time.time() - last_keepalive > 8:
                loop.run_in_executor(None, cam_cgi, "mode=getstate")
                last_keepalive = time.time()

            try:
                data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=0.5)
                soi = data.find(b"\xff\xd8")
                if soi != -1:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data[soi:] + b"\r\n"
            except asyncio.TimeoutError:
                continue
    finally:
        sock.close()


@app.get("/api/stream")
async def video_stream():
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/files")
def list_files(start: int = 0, count: int = 100):
    cam_cgi("mode=camcmd&value=playmode")
    time.sleep(0.6)

    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
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

    try:
        r = requests.post(f"http://{CAM_IP}:60606/Server0/CDS_control", data=soap_body, headers=headers, timeout=5)
        root = ET.fromstring(r.text)
        didl_xml = root.find(".//Result").text
        total = root.find(".//TotalMatches").text
        didl_root = ET.fromstring(didl_xml)

        ns = {"didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/", "dc": "http://purl.org/dc/elements/1.1/"}
        items = []

        for item in didl_root.findall("didl:item", ns):
            title = item.find("dc:title", ns).text
            res_list = item.findall("didl:res", ns)
            orig_url, thumb_url = None, None

            for res in res_list:
                url = res.text
                proto = res.attrib.get("protocolInfo", "")
                if "JPEG_TN" in proto or "JPEG_SM" in proto:
                    thumb_url = url
                elif orig_url is None:
                    orig_url = url

            items.append({
                "title": title,
                "original": orig_url or (res_list[0].text if res_list else ""),
                "thumb": thumb_url or orig_url
            })

        return {"total": int(total or 0), "items": items}
    except Exception as e:
        return {"error": str(e), "items": []}