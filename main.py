import os,re,uuid,asyncio,secrets,ipaddress
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI,Request,Form,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse,FileResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine,text
from passlib.context import CryptContext
from yt_dlp import YoutubeDL
DB=os.getenv('DATABASE_URL','')
if not DB: raise RuntimeError('DATABASE_URL is required. Create PostgreSQL in Render.')
DB=DB.replace('postgres://','postgresql+psycopg://',1)
if DB.startswith('postgresql://'): DB=DB.replace('postgresql://','postgresql+psycopg://',1)
engine=create_engine(DB,pool_pre_ping=True); pwd=CryptContext(schemes=['bcrypt'],deprecated='auto')
app=FastAPI(title='VidNova'); app.add_middleware(SessionMiddleware,secret_key=os.getenv('SECRET_KEY') or secrets.token_urlsafe(48),same_site='lax',https_only=True)
LIMIT=10; MAX_MB=int(os.getenv('MAX_FILE_MB','500')); OUT=Path('/tmp/vidnova'); OUT.mkdir(exist_ok=True); jobs={}; lock=asyncio.Lock()
with engine.begin() as c:
 c.execute(text('CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY,email TEXT UNIQUE NOT NULL,username TEXT UNIQUE NOT NULL,password_hash TEXT,google_sub TEXT UNIQUE,created_at TIMESTAMPTZ DEFAULT NOW())'))
 c.execute(text('CREATE TABLE IF NOT EXISTS downloads(id BIGSERIAL PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,created_at TIMESTAMPTZ DEFAULT NOW(),title TEXT NOT NULL,format TEXT NOT NULL)'))
 c.execute(text('CREATE INDEX IF NOT EXISTS downloads_day_idx ON downloads(user_id,created_at)'))
def get_user(req):
 uid=req.session.get('uid')
 if not uid:return None
 with engine.connect() as c:r=c.execute(text('SELECT id,email,username FROM users WHERE id=:i'),{'i':uid}).mappings().first()
 return dict(r) if r else None
def need_user(req):
 u=get_user(req)
 if not u:raise HTTPException(401,'Sign in or create an account first.')
 return u
def used(uid):
 with engine.connect() as c:return c.execute(text("SELECT COUNT(*) FROM downloads WHERE user_id=:u AND created_at>=date_trunc('day',NOW())"),{'u':uid}).scalar_one()
def valid_url(s):
 try:
  p=urlparse(s.strip()); h=p.hostname
  if p.scheme not in ('http','https') or not h or h=='localhost' or h.endswith(('.local','.internal')):raise ValueError()
  try:
   ip=ipaddress.ip_address(h)
   if not ip.is_global:raise ValueError()
  except ValueError:
   if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}',h):raise ValueError()
  return s.strip()
 except Exception:raise HTTPException(400,'Enter a valid public http(s) video URL.')
@app.get('/',response_class=HTMLResponse)
def home(req:Request):
 u=get_user(req); left=max(0,LIMIT-used(u['id'])) if u else 0
 return HTMLResponse(HTML.replace('{{GOOGLE}}','true' if os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET') else 'false'))
@app.post('/register')
def register(req:Request,email:str=Form(...),username:str=Form(...),password:str=Form(...),confirm:str=Form(...)):
 email=email.strip().lower();username=username.strip()
 if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):raise HTTPException(400,'Enter a valid email.')
 if not re.fullmatch(r'[A-Za-z0-9_.-]{3,24}',username):raise HTTPException(400,'Username must be 3–24 characters.')
 if len(password)<10:raise HTTPException(400,'Password must contain at least 10 characters.')
 if password!=confirm:raise HTTPException(400,'Passwords do not match.')
 try:
  with engine.begin() as c:uid=c.execute(text('INSERT INTO users(email,username,password_hash) VALUES(:e,:u,:p) RETURNING id'),{'e':email,'u':username,'p':pwd.hash(password)}).scalar_one()
 except Exception as e:
  if 'unique' in str(e).lower():raise HTTPException(409,'Email or username already exists.')
  raise
 req.session.clear();req.session['uid']=uid;return RedirectResponse('/',303)
@app.post('/login')
def login(req:Request,identity:str=Form(...),password:str=Form(...)):
 with engine.connect() as c:r=c.execute(text('SELECT id,password_hash FROM users WHERE lower(email)=:v OR lower(username)=:v LIMIT 1'),{'v':identity.strip().lower()}).mappings().first()
 if not r or not r['password_hash'] or not pwd.verify(password,r['password_hash']):raise HTTPException(401,'Incorrect login or password.')
 req.session.clear();req.session['uid']=r['id'];return RedirectResponse('/',303)
@app.post('/logout')
def logout(req:Request):req.session.clear();return RedirectResponse('/',303)
@app.get('/api/me')
def me(req:Request):
 u=get_user(req)
 if not u:return {'authenticated':False}
 n=used(u['id']);return {'authenticated':True,'username':u['username'],'remaining':max(0,LIMIT-n),'limit':LIMIT}
@app.post('/api/analyze')
async def analyze(req:Request):
 u=need_user(req)
 if used(u['id'])>=LIMIT:raise HTTPException(429,'Daily limit reached: 10 successful downloads per account per day.')
 url=valid_url(str((await req.json()).get('url','')))
 def inspect():
  with YoutubeDL({'quiet':True,'no_warnings':True,'noplaylist':True,'skip_download':True,'socket_timeout':15}) as y:return y.extract_info(url,download=False)
 try:i=await asyncio.to_thread(inspect)
 except Exception:raise HTTPException(422,'Could not analyze this link: it may be private, restricted, unavailable or unsupported.')
 fs=[];seen=set()
 for f in i.get('formats',[]):
  if f.get('ext') not in ('mp4','webm','m4a','mp3') or (f.get('vcodec')=='none' and f.get('acodec')=='none'):continue
  fid=f.get('format_id')
  if fid in seen:continue
  seen.add(fid);fs.append({'format_id':fid,'ext':f['ext'],'label':f"{f.get('height')}p" if f.get('height') else ('Audio' if f.get('vcodec')=='none' else f['ext'].upper())})
 return {'title':i.get('title','Video'),'thumbnail':i.get('thumbnail'),'duration':i.get('duration'),'formats':fs[:35]}
@app.post('/api/download')
async def start(req:Request):
 u=need_user(req)
 if used(u['id'])>=LIMIT:raise HTTPException(429,'Daily limit reached: 10 successful downloads per account per day.')
 b=await req.json();url=valid_url(str(b.get('url','')));fmt=str(b.get('format_id','best'))
 if not re.fullmatch(r'[A-Za-z0-9+_.-]{1,80}',fmt):raise HTTPException(400,'Invalid format.')
 async with lock:
  if any(j['uid']==u['id'] and j['status'] in ('queued','working') for j in jobs.values()):raise HTTPException(409,'One download is already in progress.')
  jid=uuid.uuid4().hex;jobs[jid]={'uid':u['id'],'status':'queued','path':None,'title':'Preparing video','error':None}
 asyncio.create_task(download_job(jid,u['id'],url,fmt));return {'job_id':jid}
async def download_job(jid,uid,url,fmt):
 try:
  jobs[jid]['status']='working'
  def do():
   opts={'quiet':True,'no_warnings':True,'noplaylist':True,'socket_timeout':20,'retries':2,'format':fmt if fmt!='best' else 'bestvideo*+bestaudio/best','merge_output_format':'mp4','outtmpl':str(OUT/'%(id)s-%(title).70s.%(ext)s'),'max_filesize':MAX_MB*1024*1024,'restrictfilenames':True}
   with YoutubeDL(opts) as y:i=y.extract_info(url,download=True);name=y.prepare_filename(i)
   files=[Path(name)]+list(OUT.glob(f"{i.get('id','')}*"));f=next((x for x in files if x.is_file() and x.suffix.lower() in ('.mp4','.webm','.mkv','.m4a','.mp3')),None)
   if not f:raise RuntimeError('Output file was not found.')
   if f.stat().st_size>MAX_MB*1024*1024:f.unlink(missing_ok=True);raise RuntimeError(f'File exceeds {MAX_MB} MB.')
   return str(f),str(i.get('title','Video')),f.suffix.lstrip('.')
  path,title,ext=await asyncio.to_thread(do)
  with engine.begin() as c:
   n=c.execute(text("SELECT COUNT(*) FROM downloads WHERE user_id=:u AND created_at>=date_trunc('day',NOW())"),{'u':uid}).scalar_one()
   if n>=LIMIT:Path(path).unlink(missing_ok=True);raise RuntimeError('Daily limit reached.')
   c.execute(text('INSERT INTO downloads(user_id,title,format) VALUES(:u,:t,:f)'),{'u':uid,'t':title[:500],'f':ext})
  jobs[jid].update(status='done',path=path,title=title)
 except Exception as e:jobs[jid].update(status='error',error=str(e)[:250] or 'Download failed.')
@app.get('/api/jobs/{jid}')
def job(req:Request,jid:str):
 u=need_user(req);j=jobs.get(jid)
 if not j or j['uid']!=u['id']:raise HTTPException(404,'Job not found.')
 return {'status':j['status'],'title':j['title'],'error':j['error'],'download_url':f'/api/jobs/{jid}/file' if j['status']=='done' else None}
@app.get('/api/jobs/{jid}/file')
def file(req:Request,jid:str):
 u=need_user(req);j=jobs.get(jid)
 if not j or j['uid']!=u['id'] or j['status']!='done' or not j.get('path'):raise HTTPException(404,'File not ready or expired.')
 f=Path(j['path'])
 if not f.is_file():raise HTTPException(410,'Temporary file expired.')
 return FileResponse(f,filename='vidnova-'+f.name,media_type='application/octet-stream')
@app.get('/health')
def health():return {'status':'ok'}

# Optional Google OAuth: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to enable.
try:
 from authlib.integrations.starlette_client import OAuth
 oauth=OAuth()
 if os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'):
  oauth.register(name='google',client_id=os.getenv('GOOGLE_CLIENT_ID'),client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',client_kwargs={'scope':'openid email profile'})
  @app.get('/auth/google')
  async def google_login(req:Request):return await oauth.google.authorize_redirect(req,req.url_for('google_callback'))
  @app.get('/auth/google/callback',name='google_callback')
  async def google_callback(req:Request):
   token=await oauth.google.authorize_access_token(req);info=token.get('userinfo') or await oauth.google.parse_id_token(req,token);email=(info.get('email') or '').lower();sub=info.get('sub')
   if not email or not sub or not info.get('email_verified'):raise HTTPException(400,'A verified Google email is required.')
   base=re.sub(r'[^A-Za-z0-9_.-]','',(info.get('name') or email.split('@')[0]))[:24] or 'user';
   with engine.begin() as c:
    row=c.execute(text('SELECT id FROM users WHERE google_sub=:s OR lower(email)=:e'),{'s':sub,'e':email}).first()
    if row:uid=row[0];c.execute(text('UPDATE users SET google_sub=COALESCE(google_sub,:s) WHERE id=:i'),{'s':sub,'i':uid})
    else:
     name=base;k=1
     while c.execute(text('SELECT 1 FROM users WHERE lower(username)=lower(:u)'),{'u':name}).first():name=base[:20]+str(k);k+=1
     uid=c.execute(text('INSERT INTO users(email,username,google_sub) VALUES(:e,:u,:s) RETURNING id'),{'e':email,'u':name,'s':sub}).scalar_one()
   req.session.clear();req.session['uid']=uid;return RedirectResponse('/',303)
except ImportError:pass

HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VidNova — Video Downloader</title><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"><style>
:root{color-scheme:dark;--bg:#090b12;--line:#252b3b;--text:#f4f6ff;--muted:#9aa3b8;--accent:#8c73ff;--cyan:#4fe2cf}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -20%,#292050 0,transparent 45%),var(--bg);color:var(--text);font:15px Inter,system-ui,sans-serif;min-height:100vh}header{height:74px;border-bottom:1px solid #ffffff12;display:flex;align-items:center;justify-content:space-between;padding:0 max(20px,calc((100% - 1080px)/2));gap:12px}.brand{font-size:22px;font-weight:800;letter-spacing:-1px}.brand span{color:var(--cyan)}.pill{padding:7px 10px;background:#1d2540;border:1px solid #38355f;border-radius:999px;color:#d5d0ff;font-size:12px}.btn{border:0;border-radius:12px;padding:13px 17px;font-weight:700;cursor:pointer;background:linear-gradient(135deg,var(--accent),#6652e7);color:white}.secondary{background:#1b2030;border:1px solid var(--line)}main{max-width:920px;margin:auto;padding:65px 20px 40px}.eyebrow{text-align:center;color:var(--cyan);font-size:11px;letter-spacing:2.5px;font-weight:800}.hero h1{text-align:center;font-size:clamp(37px,6vw,58px);line-height:1.05;letter-spacing:-2.8px;margin:16px 0}.hero h1 span{color:#a89aff}.hero p{text-align:center;color:var(--muted);line-height:1.7;max-width:610px;margin:auto}.card{background:linear-gradient(145deg,#151a29,#10131d);border:1px solid var(--line);border-radius:22px;padding:23px;margin:34px auto 0;box-shadow:0 24px 70px #0005}.row{display:flex;gap:10px}.row input,input,select{width:100%;min-width:0;background:#0b0e17;border:1px solid #30374b;border-radius:12px;padding:14px;color:var(--text);outline:none}.hint{color:var(--muted);font-size:12px;margin:11px 3px 0;line-height:1.6}.result{display:none;margin-top:22px;padding-top:20px;border-top:1px solid var(--line)}.preview{display:flex;gap:15px;align-items:center}.preview img{width:145px;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:#202438}.preview h3{font-size:16px;line-height:1.4;margin:0 0 7px}.choices{display:flex;gap:10px;margin-top:18px}.choices select{flex:1}.status{margin-top:16px;color:var(--muted);line-height:1.6;overflow-wrap:anywhere}.meter{margin-top:17px;display:flex;justify-content:space-between;gap:12px;align-items:center;color:var(--muted);font-size:13px}.auth{display:none}.authgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.authbox{background:#0d111b;border:1px solid var(--line);padding:18px;border-radius:16px}.authbox h3{margin:0 0 14px}.authbox form{display:grid;gap:10px}.authbox input{padding:12px}.msg{color:#ffb9b9;font-size:13px;margin-top:12px}.bar{display:flex;align-items:center;gap:10px;font-size:13px}.logout{border:1px solid var(--line);background:transparent;color:var(--muted);padding:8px 10px;border-radius:9px;cursor:pointer}.google{display:block;text-align:center;text-decoration:none;color:white;background:#202638;padding:12px;border-radius:10px;margin-top:12px;border:1px solid var(--line)}footer{text-align:center;color:#747d91;font-size:12px;line-height:1.8;padding:25px 12px}@media(max-width:640px){main{padding-top:42px}.card{padding:16px;margin-top:25px}.row,.choices{flex-direction:column}.preview{align-items:flex-start}.preview img{width:105px}.authgrid{grid-template-columns:1fr}.hero h1{letter-spacing:-1.6px}header{height:65px}.bar{gap:6px}}
</style></head><body><header><div class="brand">vid<span>nova</span> ✦</div><div class="bar"><span id="welcome"></span><span class="pill" id="toplimit"></span><button class="logout" id="logout" style="display:none">Log out</button><button class="btn secondary" id="signinbtn">Sign in</button></div></header><main><section class="hero"><div class="eyebrow">YOUR MEDIA, YOUR WAY</div><h1>Save the videos<br><span>you love.</span></h1><p>Paste a public video link, choose a format and quality, and get your file. Simple, fast, and in one place.</p></section><section class="card"><div class="row"><input id="url" type="url" placeholder="Paste a video URL here…"><button class="btn" id="analyze">Analyze link ↗</button></div><div class="hint">Download only videos you own or have permission to save. Private, DRM-protected and restricted content is not supported.</div><div class="result" id="result"><div class="preview"><img id="thumb" alt="Video thumbnail"><div><h3 id="title"></h3><small id="duration"></small></div></div><div class="choices"><select id="format"></select><button class="btn" id="download">Download file ↓</button></div></div><div class="status" id="status"></div><div class="meter"><span>Daily free downloads</span><span class="pill" id="limit">Sign in to start · 10 per day</span></div></section><section class="card auth" id="auth"><div class="authgrid"><div class="authbox"><h3>Create account</h3><form id="register"><input name="email" type="email" placeholder="Email" required><input name="username" placeholder="Username (3–24 chars)" minlength="3" maxlength="24" required><input name="password" type="password" placeholder="Password (10+ characters)" minlength="10" required><input name="confirm" type="password" placeholder="Confirm password" minlength="10" required><button class="btn">Create account</button></form></div><div class="authbox"><h3>Welcome back</h3><form id="login"><input name="identity" placeholder="Email or username" required><input name="password" type="password" placeholder="Password" required><button class="btn">Sign in</button></form><div id="googlebox"></div></div></div><div class="msg" id="authmsg"></div></section><footer>10 successful downloads per account each day · Files are temporary and may disappear after a restart.<br>Use responsibly and respect copyright and source-platform terms.</footer></main><script>
let me={},sourceUrl='',jobId='';const $=x=>document.getElementById(x);async function api(url,opt={}){let r=await fetch(url,{...opt,headers:{'Content-Type':'application/json',...(opt.headers||{})}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.detail||'Request failed');return d}async function refresh(){me=await api('/api/me');let yes=me.authenticated;$('welcome').textContent=yes?'Hi, '+me.username:'';$('toplimit').textContent=yes?me.remaining+'/10 left today':'10 free / day';$('limit').textContent=yes?me.remaining+' of 10 remaining today':'Sign in to start · 10 per day';$('signinbtn').style.display=yes?'none':'block';$('logout').style.display=yes?'block':'none';$('googlebox').innerHTML=window.GOOGLE_ENABLED?'<a class="google" href="/auth/google">Continue with Google</a>':''}refresh().catch(()=>{});$('signinbtn').onclick=()=>$('auth').style.display=$('auth').style.display==='block'?'none':'block';$('logout').onclick=async()=>{await fetch('/logout',{method:'POST'});location.reload()};$('analyze').onclick=async()=>{if(!me.authenticated){$('auth').style.display='block';$('status').textContent='Create an account or sign in to continue.';return}sourceUrl=$('url').value.trim();$('status').textContent='Analyzing link…';$('result').style.display='none';try{let d=await api('/api/analyze',{method:'POST',body:JSON.stringify({url:sourceUrl})});$('title').textContent=d.title;$('thumb').src=d.thumbnail||'';$('duration').textContent=d.duration?Math.floor(d.duration/60)+' min '+d.duration%60+' sec':'Duration unavailable';$('format').innerHTML='<option value="best">Best available quality (MP4 where possible)</option>'+d.formats.map(f=>`<option value="${f.format_id}">${f.label} · ${f.ext}</option>`).join('');$('result').style.display='block';$('status').textContent='Choose a format and start your download.'}catch(e){$('status').textContent=e.message}};$('download').onclick=async()=>{try{if(!sourceUrl)throw Error('Analyze a link first.');$('download').disabled=true;$('status').textContent='Starting download…';let d=await api('/api/download',{method:'POST',body:JSON.stringify({url:sourceUrl,format_id:$('format').value})});jobId=d.job_id;while(true){await new Promise(r=>setTimeout(r,1800));let j=await api('/api/jobs/'+jobId);if(j.status==='error')throw Error(j.error||'Download failed');if(j.status==='done'){$('status').innerHTML='File ready: <a style="color:#75ead7" href="'+j.download_url+'">Download file ↓</a>';break}$('status').textContent=j.status==='queued'?'Waiting…':'Downloading and preparing your file…'}await refresh()}catch(e){$('status').textContent=e.message}finally{$('download').disabled=false}};async function postForm(form,path){let r=await fetch(path,{method:'POST',body:new FormData(form)});if(!r.ok){let d={};try{d=await r.json()}catch{}throw Error(d.detail||'Request failed')}location.reload()}$('register').onsubmit=async e=>{e.preventDefault();try{await postForm(e.target,'/register')}catch(x){$('authmsg').textContent=x.message}};$('login').onsubmit=async e=>{e.preventDefault();try{await postForm(e.target,'/login')}catch(x){$('authmsg').textContent=x.message}};window.GOOGLE_ENABLED={{GOOGLE}};
</script></body></html>'''

