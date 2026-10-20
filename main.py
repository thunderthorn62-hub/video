import os, re, uuid, hmac, hashlib, secrets, asyncio, ipaddress, socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
import yt_dlp
import imageio_ffmpeg

# VidNova: MongoDB Atlas + FastAPI + yt-dlp
# Render environment: DATABASE_URL, SECRET_KEY; optional MONGODB_DB_NAME, MAX_FILE_MB
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
SECRET_KEY = os.getenv('SECRET_KEY', '').strip()
DB_NAME = os.getenv('MONGODB_DB_NAME', 'vidnova')
MAX_FILE_MB = int(os.getenv('MAX_FILE_MB', '500'))
DAILY_LIMIT = 10
DOWNLOAD_DIR = Path('/tmp/vidnova')
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
client = db = None
jobs = {}
lock = asyncio.Lock()
bearer = HTTPBearer(auto_error=False)

@asynccontextmanager
async def lifespan(app):
    global client, db
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL is missing. Set your MongoDB Atlas URI in Render Environment.')
    if len(SECRET_KEY) < 32:
        raise RuntimeError('SECRET_KEY must be set to a random string of at least 32 characters.')
    client = AsyncIOMotorClient(DATABASE_URL, serverSelectionTimeoutMS=15000, connectTimeoutMS=15000, appname='VidNova')
    try:
        await client.admin.command('ping')
        db = client[DB_NAME]
        await db.users.create_index('email', unique=True)
        await db.users.create_index('token_hash', sparse=True)
        await db.downloads.create_index([('user_id', 1), ('created_at', -1)])
    except Exception:
        client.close(); client = db = None
        raise
    yield
    if client: client.close()

app = FastAPI(title='VidNova', version='2.0.0', lifespan=lifespan)

class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
class URLBody(BaseModel):
    url: str
class DownloadBody(URLBody):
    format: str = 'mp4'
    quality: str = 'best'

def now(): return datetime.now(timezone.utc)
def pw_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + '$' + hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 310000).hex()
def pw_ok(password, stored):
    try:
        salt, digest = stored.split('$', 1)
        return hmac.compare_digest(pw_hash(password, bytes.fromhex(salt)).split('$', 1)[1], digest)
    except Exception: return False
def digest(token): return hmac.new(SECRET_KEY.encode(), token.encode(), hashlib.sha256).hexdigest()
def public_user(u): return {'id': str(u['_id']), 'email': u['email'], 'created_at': u.get('created_at')}

async def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = credentials.credentials if credentials else request.cookies.get('vidnova_token')
    if not token: raise HTTPException(401, 'Please sign in.')
    user = await db.users.find_one({'token_hash': digest(token)})
    if not user: raise HTTPException(401, 'Session expired. Please sign in again.')
    return user

def check_url(url):
    url = url.strip(); p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname: raise HTTPException(400, 'Enter a valid http:// or https:// URL.')
    host = p.hostname.lower().rstrip('.')
    if host in ('localhost', 'localhost.localdomain') or host.endswith('.local'): raise HTTPException(400, 'This URL is not allowed.')
    try:
        for item in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(item[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                raise HTTPException(400, 'This URL is not allowed.')
    except socket.gaierror: pass
    return url

async def used_today(uid):
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    return await db.downloads.count_documents({'user_id': uid, 'created_at': {'$gte': start}, 'status': 'success'})

def get_info(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': False,
        'skip_download': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 3,
        'verbose': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        x = ydl.extract_info(url, download=False)

    return {
        'title': x.get('title') or 'Untitled',
        'duration': x.get('duration'),
        'thumbnail': x.get('thumbnail'),
        'extractor': x.get('extractor_key'),
        'webpage_url': x.get('webpage_url') or url,
        'filesize': x.get('filesize') or x.get('filesize_approx')
    }

def download_sync(jid, url, fmt, quality):
    job = jobs[jid]
    template = str(DOWNLOAD_DIR / f'{jid}.%(ext)s')

    opts = {
        'quiet': True,
        'no_warnings': False,
        'noplaylist': True,
        'outtmpl': template,
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        'socket_timeout': 30,
        'retries': 3,
        'max_filesize': MAX_FILE_MB * 1024 * 1024,
        'overwrites': True,
        'verbose': True,
    }
    if fmt == 'mp3':
        opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}]})
    elif fmt == 'webm': opts.update({'format':'bestvideo[ext=webm]+bestaudio[ext=webm]/best[ext=webm]/best','merge_output_format':'webm'})
    else: opts.update({'format':'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best','merge_output_format':'mp4'})
    if quality in ('360','480','720','1080') and fmt != 'mp3': opts['format'] = f'best[height<={quality}]/best'
    try:
        job['status'] = 'processing'
        with yt_dlp.YoutubeDL(opts) as ydl: info = ydl.extract_info(url, download=True)
        files = [p for p in DOWNLOAD_DIR.glob(f'{jid}.*') if p.is_file() and p.suffix not in ('.part','.ytdl')]
        if not files: raise RuntimeError('Downloaded file was not found.')
        file = max(files, key=lambda p:p.stat().st_size)
        if file.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            file.unlink(missing_ok=True); raise RuntimeError(f'File exceeds {MAX_FILE_MB} MB limit.')
        job.update(status='completed', title=info.get('title') or 'download', filename=file.name, size=file.stat().st_size, file_path=str(file), _finished=True)
    except Exception as e:
        for p in DOWNLOAD_DIR.glob(f'{jid}.*'):
            try: p.unlink(missing_ok=True)
            except Exception: pass
        job.update(status='failed', error=str(e)[:400], _finished=True)

async def run_job(jid, user, url, fmt, quality):
    if await used_today(user['_id']) >= DAILY_LIMIT:
        jobs[jid].update(status='failed', error='Daily limit reached (10 successful downloads).', _finished=True); return
    await asyncio.to_thread(download_sync, jid, url, fmt, quality)
    j = jobs[jid]
    if j.get('status') == 'completed':
        await db.downloads.insert_one({'user_id': user['_id'], 'job_id': jid, 'created_at': now(), 'status':'success', 'format':fmt, 'title':j.get('title')})
    await db.jobs.update_one({'_id':jid}, {'$set':{'status':j.get('status'), 'title':j.get('title'), 'error':j.get('error'), 'filename':j.get('filename'), 'size':j.get('size'), 'updated_at':now()}}, upsert=True)

from pathlib import Path
from fastapi.responses import HTMLResponse

BASE_DIR = Path(__file__).resolve().parent

@app.get("/", response_class=HTMLResponse)
async def home():
    html_file = BASE_DIR / "index.html"

    if not html_file.exists():
        return HTMLResponse(
            "<h1>VidNova</h1><p>index.html not found</p>",
            status_code=500
        )

    return HTMLResponse(
        html_file.read_text(encoding="utf-8")
    )

@app.get('/health')
async def health():
    try:
        await db.command('ping'); return {'status':'ok','database':'connected','database_name':DB_NAME}
    except Exception as e: return JSONResponse(status_code=503, content={'status':'error','database':'unavailable','detail':str(e)[:200]})

@app.post('/api/register')
async def register(body: RegisterBody, response: Response):
    email = str(body.email).lower().strip(); token = secrets.token_urlsafe(40)
    doc = {'email':email,'password_hash':pw_hash(body.password),'token_hash':digest(token),'created_at':now()}
    try: result = await db.users.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, 'An account with this email already exists.')
    doc['_id'] = result.inserted_id
    response.set_cookie('vidnova_token', token, httponly=True, secure=True, samesite='lax', max_age=2592000, path='/')
    return {'ok':True,'user':public_user(doc),'token':token}

@app.post('/api/login')
async def login(body: LoginBody, response: Response):
    user = await db.users.find_one({'email':str(body.email).lower().strip()})
    if not user or not pw_ok(body.password, user['password_hash']): raise HTTPException(401, 'Incorrect email or password.')
    token = secrets.token_urlsafe(40)
    await db.users.update_one({'_id':user['_id']}, {'$set':{'token_hash':digest(token)}})
    response.set_cookie('vidnova_token', token, httponly=True, secure=True, samesite='lax', max_age=2592000, path='/')
    return {'ok':True,'user':public_user(user),'token':token}

@app.post('/api/logout')
async def logout(request: Request, response: Response, user=Depends(current_user)):
    token = request.cookies.get('vidnova_token')
    if token: await db.users.update_one({'_id':user['_id'],'token_hash':digest(token)}, {'$unset':{'token_hash':''}})
    response.delete_cookie('vidnova_token', path='/'); return {'ok':True}

@app.get('/api/me')
async def me(user=Depends(current_user)):
    return {'user':public_user(user),'downloads_today':await used_today(user['_id']),'daily_limit':DAILY_LIMIT}

@app.post('/api/analyze')
async def analyze(body: URLBody, user=Depends(current_user)):
    url = check_url(body.url)
    try: info = await asyncio.to_thread(get_info, url)
    except Exception as e: raise HTTPException(422, f'Could not analyze this URL: {str(e)[:250]}')
    return {'ok':True, **info, 'downloads_today':await used_today(user['_id']), 'daily_limit':DAILY_LIMIT}

@app.post('/api/download')
async def create_download(body: DownloadBody, user=Depends(current_user)):
    url = check_url(body.url); fmt = body.format.lower().strip()
    if fmt not in ('mp4','mp3','webm'): raise HTTPException(400, 'Format must be mp4, mp3, or webm.')
    quality = str(body.quality).lower().strip()
    if quality not in ('best','360','480','720','1080'): quality = 'best'
    if await used_today(user['_id']) >= DAILY_LIMIT: raise HTTPException(429, 'Daily limit reached: 10 successful downloads per day.')
    jid = uuid.uuid4().hex
    async with lock: jobs[jid] = {'job_id':jid,'user_id':str(user['_id']),'status':'queued','created_at':now(),'format':fmt}
    await db.jobs.insert_one({'_id':jid,'user_id':str(user['_id']),'status':'queued','created_at':now()})
    asyncio.create_task(run_job(jid, user, url, fmt, quality))
    return {'ok':True,'job_id':jid,'status':'queued'}

@app.get('/api/jobs/{jid}')
async def get_job(jid: str, user=Depends(current_user)):
    j = jobs.get(jid)
    if not j or j.get('user_id') != str(user['_id']): raise HTTPException(404, 'Job not found.')
    return {'job_id':jid,'status':j.get('status'),'title':j.get('title'),'format':j.get('format'),'size':j.get('size'),'error':j.get('error'),'download_url':f'/api/jobs/{jid}/file' if j.get('status')=='completed' else None}

@app.get('/api/jobs/{jid}/file')
async def job_file(jid: str, user=Depends(current_user)):
    j = jobs.get(jid)
    if not j or j.get('user_id') != str(user['_id']): raise HTTPException(404, 'Job not found.')
    if j.get('status') != 'completed' or not j.get('file_path'): raise HTTPException(409, 'The file is not ready.')
    p = Path(j['file_path'])
    if not p.exists() or p.parent != DOWNLOAD_DIR: raise HTTPException(404, 'File expired. Please download again.')
    title = re.sub(r'[^a-zA-Z0-9._ -]+','_',j.get('title') or 'vidnova')[:100]
    return FileResponse(p, filename=f'{title}{p.suffix}', media_type='application/octet-stream')

@app.exception_handler(Exception)
async def generic_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={'detail':'Internal server error. Check Render logs.'})
 
