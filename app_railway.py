"""Railway-compatible Telegram bot runner. Uploaded code runs in the same service, not isolated containers."""
import asyncio, json, os, shutil, signal, subprocess, sys, time, zipfile
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# APNI DETAILS YAHAN CODE MEIN BHARO — Railway Variables zaroori nahi.
# BOT_TOKEN ko public GitHub repository mein upload/share mat karna.
BOT_TOKEN = "8954438874:AAHmsruBC7R7-e2iJaGgOVT1c-XKkIOfJgI"
OWNER_ID = 8758092321  # Is number ko apni numeric Telegram user ID se badlo

# Agar Railway Variables set kiye hain, to woh in values par priority lenge.
BOT_TOKEN = os.getenv("BOT_TOKEN", BOT_TOKEN)
OWNER_ID = int(os.getenv("OWNER_ID", str(OWNER_ID)))
BASE_DIR = Path(os.getenv('DEPLOY_BASE_DIR', '/app/runtime_apps'))
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '10'))
MAX_EXTRACTED_MB = int(os.getenv('MAX_EXTRACTED_MB', '50'))
BASE_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = BASE_DIR / 'deployments.json'
STATE, PROCESSES = {}, {}
try: DEPLOYMENTS = json.loads(DB_FILE.read_text()) if DB_FILE.exists() else {}
except Exception: DEPLOYMENTS = {}

def save_db(): DB_FILE.write_text(json.dumps(DEPLOYMENTS, indent=2))
def is_owner(uid): return OWNER_ID > 0 and uid == OWNER_ID
def main_keyboard(): return ReplyKeyboardMarkup([['DEPLOY BOT', 'MY BOT'], ['BOT OWNER']], resize_keyboard=True)
def manage_keyboard(did):
    return InlineKeyboardMarkup([[InlineKeyboardButton('START', callback_data=f'start:{did}'), InlineKeyboardButton('STOP', callback_data=f'stop:{did}')], [InlineKeyboardButton('LOGS', callback_data=f'logs:{did}'), InlineKeyboardButton('DELETE', callback_data=f'delete:{did}')]])

def safe_extract_zip(zpath, dest):
    total, root = 0, dest.resolve()
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            member = Path(info.filename)
            if member.is_absolute() or '..' in member.parts: raise ValueError('ZIP contains an unsafe path.')
            if ((info.external_attr >> 16) & 0o170000) == 0o120000: raise ValueError('ZIP symlinks are not allowed.')
            total += info.file_size
            if total > MAX_EXTRACTED_MB * 1024 * 1024: raise ValueError('Extracted ZIP is too large.')
            target = (dest / member).resolve()
            if root not in target.parents and target != root: raise ValueError('ZIP contains an unsafe path.')
            if info.is_dir(): target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open('wb') as dst: shutil.copyfileobj(src, dst)

def detect_project(folder):
    for name, runtime in [('app.py', 'python'), ('index.js', 'node')]:
        path = folder / name
        if path.is_file(): return runtime, path
    py, js = list(folder.glob('*.py')), list(folder.glob('*.js'))
    if len(py) == 1: return 'python', py[0]
    if len(js) == 1: return 'node', js[0]
    raise ValueError('ZIP must contain app.py or index.js (or exactly one .py/.js file).')

async def prepare_project(folder, runtime):
    if runtime == 'python' and (folder / 'requirements.txt').is_file():
        cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(folder / 'requirements.txt')]
    elif runtime == 'node' and (folder / 'package.json').is_file(): cmd = ['npm', 'install', '--omit=dev', '--no-audit', '--no-fund']
    else: return
    result = await asyncio.to_thread(subprocess.run, cmd, cwd=str(folder), capture_output=True, text=True, timeout=240)
    if result.returncode: raise RuntimeError((result.stderr or result.stdout)[-2000:])

def pid_alive(pid):
    try: os.kill(int(pid), 0); return True
    except (ProcessLookupError, PermissionError, ValueError, TypeError): return False

def running(did):
    proc = PROCESSES.get(did)
    if proc and proc.poll() is None: return True
    return bool(DEPLOYMENTS.get(did, {}).get('pid') and pid_alive(DEPLOYMENTS[did]['pid']))

def start_process(did):
    r = DEPLOYMENTS[did]; folder = Path(r['path']); log = open(r['log_path'], 'ab')
    cmd = [sys.executable, r['entry']] if r['runtime'] == 'python' else ['node', r['entry']]
    proc = subprocess.Popen(cmd, cwd=str(folder), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close(); PROCESSES[did] = proc; r.update(pid=proc.pid, status='running'); save_db()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user: STATE.pop(update.effective_user.id, None)
    await update.message.reply_text('WELCOME TO THE BOT 🔥', reply_markup=main_keyboard())
async def deploy_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid): return await update.message.reply_text('ONLY THE BOT OWNER CAN DEPLOY FILES.')
    STATE[uid] = {'action': 'upload'}
    await update.message.reply_text('SEND ME YOUR .PY, .JS OR .ZIP FILE')
async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await update.message.reply_text('ONLY THE BOT OWNER CAN MANAGE DEPLOYMENTS.')
    if not DEPLOYMENTS: return await update.message.reply_text('YOU HAVE NO DEPLOYMENTS YET.')
    for did, r in list(DEPLOYMENTS.items())[-20:]:
        await update.message.reply_text(f"{r.get('name', did)}\n{'BOT IS RUNNING' if running(did) else 'BOT IS STOPPED'}", reply_markup=manage_keyboard(did))
async def owner_info(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text(f'BOT OWNER ID: {OWNER_ID}')

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id) or STATE.get(user.id, {}).get('action') != 'upload': return
    doc = update.message.document
    if not doc: return await update.message.reply_text('PLEASE SEND A .PY, .JS OR .ZIP FILE.')
    if (doc.file_size or 0) > MAX_UPLOAD_MB * 1024 * 1024: return await update.message.reply_text(f'FILE TOO LARGE. MAXIMUM {MAX_UPLOAD_MB} MB.')
    filename = Path(doc.file_name or 'upload').name; ext = Path(filename).suffix.lower()
    if ext not in {'.py', '.js', '.zip'}: return await update.message.reply_text('ONLY .PY, .JS AND .ZIP FILES ARE ALLOWED.')
    STATE.pop(user.id, None); await update.message.reply_text('WAIT SOME TIME')
    did = f'{user.id}_{int(time.time())}'; work = BASE_DIR / did; work.mkdir(parents=True, exist_ok=False); archive = work / ('upload' + ext)
    try:
        tgfile = await context.bot.get_file(doc.file_id); await tgfile.download_to_drive(custom_path=str(archive))
        if ext == '.zip':
            folder = work / 'src'; folder.mkdir(); safe_extract_zip(archive, folder); archive.unlink(missing_ok=True)
        else:
            folder = work; archive.replace(folder / filename)
        runtime, entry = detect_project(folder); await prepare_project(folder, runtime)
        DEPLOYMENTS[did] = {'owner_id': user.id, 'name': filename, 'path': str(folder), 'runtime': runtime, 'entry': entry.name, 'log_path': str(work / 'runtime.log'), 'created_at': int(time.time()), 'status': 'stopped'}
        save_db(); start_process(did)
        await update.message.reply_text('YOUR BOT HAS BEEN STARTED')
        await update.message.reply_text('BOT IS RUNNING', reply_markup=manage_keyboard(did))
    except subprocess.TimeoutExpired: await update.message.reply_text('DEPLOYMENT FAILED: dependency installation timed out.')
    except Exception as exc: await update.message.reply_text(f'DEPLOYMENT FAILED: {type(exc).__name__}: {str(exc)[:400]}')

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_owner(q.from_user.id): return await q.message.reply_text('ONLY THE BOT OWNER CAN MANAGE DEPLOYMENTS.')
    try: action, did = q.data.split(':', 1)
    except (AttributeError, ValueError): return
    r = DEPLOYMENTS.get(did)
    if not r: return await q.message.reply_text('DEPLOYMENT NOT FOUND.')
    if action == 'start':
        if running(did): return await q.message.reply_text('BOT IS ALREADY RUNNING.')
        try: start_process(did); await q.message.reply_text('YOUR BOT HAS BEEN STARTED.')
        except Exception as exc: await q.message.reply_text(f'START FAILED: {str(exc)[:300]}')
    elif action == 'stop':
        pid = (PROCESSES.get(did).pid if PROCESSES.get(did) and PROCESSES[did].poll() is None else r.get('pid'))
        if pid and pid_alive(pid):
            try: os.killpg(int(pid), signal.SIGTERM)
            except Exception:
                try: os.kill(int(pid), signal.SIGTERM)
                except Exception: pass
        PROCESSES.pop(did, None); r.pop('pid', None); r['status'] = 'stopped'; save_db(); await q.message.reply_text('YOUR BOT HAS BEEN STOPPED.')
    elif action == 'logs':
        path = Path(r['log_path']); content = path.read_text(errors='replace')[-9000:] if path.exists() else ''
        content = content or 'NO LOGS AVAILABLE YET.'
        for i in range(0, len(content), 3500): await q.message.reply_text('LOGS:\n' + content[i:i+3500])
    elif action == 'delete':
        pid = (PROCESSES.get(did).pid if PROCESSES.get(did) and PROCESSES[did].poll() is None else r.get('pid'))
        if pid and pid_alive(pid):
            try: os.killpg(int(pid), signal.SIGTERM)
            except Exception: pass
        PROCESSES.pop(did, None); shutil.rmtree(Path(r['path']).parent, ignore_errors=True); DEPLOYMENTS.pop(did, None); save_db(); await q.message.reply_text('YOUR CODE HAS BEEN DELETED.')

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or '').strip()
    if t == 'DEPLOY BOT': await deploy_prompt(update, context)
    elif t == 'MY BOT': await my_bots(update, context)
    elif t == 'BOT OWNER': await owner_info(update, context)
    else: await update.message.reply_text('PLEASE CHOOSE AN OPTION FROM THE MENU.')
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE): print(f'Unhandled bot error: {context.error!r}')
def main():
    if not BOT_TOKEN or BOT_TOKEN == 'PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE': raise RuntimeError('app_railway.py mein apna BOT_TOKEN bharo.')
    if OWNER_ID <= 0 or OWNER_ID == 123456789: raise RuntimeError('app_railway.py mein apni numeric OWNER_ID bharo.')
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start)); app.add_handler(CallbackQueryHandler(manage_callback, pattern=r'^(start|stop|logs|delete):'))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_file)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router)); app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)
if __name__ == '__main__': main()
