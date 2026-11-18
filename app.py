"""校园失物招领：Flask 网页、账户、审核及认领流程。"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import warnings
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

import click
from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import event, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
db = SQLAlchemy()
csrf = CSRFProtect()
CATEGORIES = ('证件卡片', '数码设备', '钥匙', '包袋衣物', '书本文具', '日用物品', '其他')
REVIEW_NAMES = {'pending': '待审核', 'approved': '已发布', 'rejected': '审核未通过', 'withdrawn': '已撤下'}
CLAIM_NAMES = {'pending': '等待核对', 'accepted': '已确认', 'rejected': '未通过'}


def now():
    # 数据库存 UTC，显示时统一转换为北京时间。
    return datetime.now(timezone.utc).replace(tzinfo=None)


@event.listens_for(Engine, 'connect')
def sqlite_settings(connection, _):
    if connection.__class__.__module__.startswith('sqlite3'):
        cursor = connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.execute('PRAGMA busy_timeout=10000')
        cursor.close()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(24), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    blocked = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    owner = db.relationship(User)
    kind = db.Column(db.String(10), nullable=False)
    title = db.Column(db.String(60), nullable=False)
    category = db.Column(db.String(20), nullable=False)
    location = db.Column(db.String(60), nullable=False)
    location_detail = db.Column(db.String(100), nullable=False, default='')
    happened_on = db.Column(db.Date, nullable=False)
    description = db.Column(db.Text, nullable=False)
    contact = db.Column(db.String(120), nullable=False)
    image_name = db.Column(db.String(48))
    review = db.Column(db.String(12), nullable=False, default='pending', index=True)
    review_note = db.Column(db.String(200), nullable=False, default='')
    resolved = db.Column(db.Boolean, nullable=False, default=False)
    # 兼容旧版数据库；旧演示记录永不进入真实认领页面。
    is_demo = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now, index=True)
    __table_args__ = (db.Index('ix_item_feed', 'review', 'resolved', 'kind', 'created_at'),)


class Claim(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    claimant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    item = db.relationship(Item, backref='claims')
    claimant = db.relationship(User)
    proof = db.Column(db.Text, nullable=False)
    contact = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(12), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, nullable=False, default=now)
    __table_args__ = (db.UniqueConstraint('item_id', 'claimant_id', name='uq_claim_person'),)


class Attempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now, index=True)


def signed_in(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            flash('请先登录，再继续操作。', 'info')
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def admin_only(view):
    @wraps(view)
    @signed_in
    def wrapped(*args, **kwargs):
        if not g.user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def create_app(test_config=None):
    load_dotenv(ROOT / '.env')
    campus = json.loads((ROOT / '校园设置.json').read_text(encoding='utf-8'))
    app = Flask(__name__, instance_path=str(ROOT / 'instance'))
    instance = Path(app.instance_path)
    instance.mkdir(exist_ok=True)
    secret = os.environ.get('SECRET_KEY')
    if not secret:
        secret_file = instance / '会话密钥.txt'
        try:
            with secret_file.open('x', encoding='utf-8') as handle:
                handle.write(secrets.token_hex(32))
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass
        except FileExistsError:
            pass
        secret = secret_file.read_text(encoding='utf-8').strip()
    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL') or 'sqlite:///' + (instance / '校园数据.db').as_posix(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={'pool_pre_ping': True, 'pool_recycle': 280},
        MAX_CONTENT_LENGTH=7 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        WTF_CSRF_TIME_LIMIT=3600,
        UPLOAD_FOLDER=str(instance / '物品图片'),
        UPLOAD_BUDGET_MB=int(os.environ.get('UPLOAD_BUDGET_MB', '120')),
        SITE_NAME=os.environ.get('SITE_NAME', campus['网站名称']),
        SCHOOL_NAME=os.environ.get('SCHOOL_NAME', campus['学校名称']),
        CAMPUS_LOCATIONS=campus['校园地点'],
    )
    if os.environ.get('ALLOWED_HOSTS'):
        app.config['TRUSTED_HOSTS'] = os.environ['ALLOWED_HOSTS'].split(',')
    if test_config:
        app.config.update(test_config)
    Path(app.config['UPLOAD_FOLDER']).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    csrf.init_app(app)

    @app.before_request
    def load_user():
        g.user = db.session.get(User, session.get('user_id')) if session.get('user_id') else None
        if g.user and g.user.blocked:
            session.clear()
            g.user = None

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        if app.config['SESSION_COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.context_processor
    def shared():
        return dict(categories=CATEGORIES, review_names=REVIEW_NAMES, claim_names=CLAIM_NAMES,
                    today=(now() + timedelta(hours=8)).date(), site_name=app.config['SITE_NAME'],
                    school_name=app.config['SCHOOL_NAME'],
                    campus_locations=app.config['CAMPUS_LOCATIONS'])

    @app.template_filter('beijing')
    def beijing(value):
        return (value + timedelta(hours=8)).strftime('%m月%d日 %H:%M')

    def field(name, minimum=1, maximum=200):
        value = request.form.get(name, '').strip()
        if not minimum <= len(value) <= maximum:
            raise ValueError(f'请检查填写内容，长度应为 {minimum} 至 {maximum} 个字符。')
        return value

    def rate_limit(scope, limit, minutes, identity=None):
        # 不信任客户端伪造的 X-Forwarded-For。平台代理下限制可能更严格。
        who = identity if identity is not None else (request.remote_addr or 'unknown')
        key = hmac.new(app.secret_key.encode(), f'{scope}:{who}'.encode(), hashlib.sha256).hexdigest()
        cutoff = now() - timedelta(minutes=minutes)
        count = db.session.scalar(select(db.func.count()).select_from(Attempt).where(Attempt.key == key, Attempt.created_at > cutoff))
        if count >= limit:
            abort(429)
        db.session.execute(db.delete(Attempt).where(Attempt.created_at < now() - timedelta(days=1)))
        db.session.add(Attempt(key=key))
        db.session.commit()

    def visible(item):
        if item.is_demo:
            return False
        return item.review == 'approved' or (g.user and (g.user.id == item.owner_id or g.user.is_admin))

    def save_image(upload):
        if not upload or not upload.filename:
            return None
        raw = upload.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            raise ValueError('图片不能超过 6 MB，请选择较小的照片。')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as source:
                    if source.format not in ('JPEG', 'PNG', 'WEBP'):
                        raise ValueError('请上传 JPG、PNG 或 WebP 图片。')
                    if source.width * source.height > 24_000_000:
                        raise ValueError('照片分辨率过大，请缩小后上传。')
                    picture = ImageOps.exif_transpose(source).convert('RGB')
                    picture.thumbnail((1280, 1280))
                    output = io.BytesIO()
                    picture.save(output, format='JPEG', quality=80, optimize=True)
                    data = output.getvalue()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError('无法读取图片，请换一张有效的 JPG、PNG 或 WebP 图片。')
        folder = Path(app.config['UPLOAD_FOLDER'])
        used = sum(p.stat().st_size for p in folder.glob('*.jpg'))
        if used + len(data) > app.config['UPLOAD_BUDGET_MB'] * 1024 * 1024:
            raise ValueError('图片空间暂时不足，可先不上传图片，并联系管理员。')
        filename = uuid4().hex + '.jpg'
        (folder / filename).write_bytes(data)
        return filename

    @app.get('/')
    def index():
        q = request.args.get('q', '').strip()[:60]
        kind = request.args.get('kind', '')
        category = request.args.get('category', '')
        location = request.args.get('location', '')
        status = request.args.get('status', 'open')
        query = select(Item).where(Item.review == 'approved', Item.is_demo.is_(False))
        if kind in ('lost', 'found'):
            query = query.where(Item.kind == kind)
        if category in CATEGORIES:
            query = query.where(Item.category == category)
        if location in app.config['CAMPUS_LOCATIONS']:
            query = query.where(Item.location == location)
        if status == 'closed':
            query = query.where(Item.resolved.is_(True))
        elif status != 'all':
            query = query.where(Item.resolved.is_(False))
        if q:
            query = query.where(or_(Item.title.contains(q, autoescape=True), Item.location.contains(q, autoescape=True), Item.description.contains(q, autoescape=True)))
        page = db.paginate(query.order_by(Item.created_at.desc(), Item.id.desc()), per_page=12, error_out=False)
        counts = dict(db.session.execute(select(Item.kind, db.func.count()).where(Item.review == 'approved', Item.resolved.is_(False), Item.is_demo.is_(False)).group_by(Item.kind)).all())
        returned = db.session.scalar(select(db.func.count()).select_from(Item).where(Item.review == 'approved', Item.resolved.is_(True), Item.is_demo.is_(False)))
        return render_template('首页.html', page=page, q=q, kind=kind, category=category, location=location, status=status, counts=counts, returned=returned)

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        error = None
        if request.method == 'POST':
            rate_limit('register', 120, 60)
            try:
                username = field('username', 2, 24)
                if not re.fullmatch(r'[\w\u4e00-\u9fff]{2,24}', username):
                    raise ValueError('昵称只能包含中文、字母、数字和下划线。')
                password = request.form.get('password', '')
                if not 10 <= len(password) <= 128:
                    raise ValueError('密码请使用 10 至 128 个字符。')
                if password != request.form.get('confirm_password'):
                    raise ValueError('两次输入的密码不一致。')
                if request.form.get('agree') != 'yes':
                    raise ValueError('请先阅读并同意使用规则与隐私说明。')
                user = User(username=username, password_hash=generate_password_hash(password))
                db.session.add(user)
                db.session.commit()
                session.clear()
                session['user_id'] = user.id
                session.permanent = True
                flash('账号已创建，现在可以发布信息。', 'success')
                return redirect(url_for('dashboard'))
            except IntegrityError:
                db.session.rollback()
                error = '这个昵称已被使用，请换一个。'
            except ValueError as exc:
                error = str(exc)
        return render_template('账户.html', mode='register', error=error), 400 if error else 200

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            rate_limit('login', 300, 15)
            username = request.form.get('username', '').strip()[:24]
            rate_limit('login-account', 12, 15, identity=username)
            user = db.session.scalar(select(User).where(User.username == username))
            password = request.form.get('password', '')
            valid = bool(user and len(password) <= 128 and check_password_hash(user.password_hash, password))
            if not valid or user.blocked:
                error = '昵称或密码错误，或账号暂时不可用。'
            else:
                session.clear()
                session['user_id'] = user.id
                session.permanent = True
                return redirect(url_for('dashboard'))
        return render_template('账户.html', mode='login', error=error), 400 if error else 200

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('index'))

    @app.route('/publish', methods=['GET', 'POST'])
    @signed_in
    def publish():
        error = None
        if request.method == 'POST':
            image_name = None
            try:
                recent = db.session.scalar(select(db.func.count()).select_from(Item).where(Item.owner_id == g.user.id, Item.created_at > now() - timedelta(days=1)))
                if recent >= 5 and not g.user.is_admin:
                    raise ValueError('每个账号每天最多发布 5 条，请明天再试。')
                kind = field('kind')
                category = field('category')
                if kind not in ('lost', 'found') or category not in CATEGORIES:
                    raise ValueError('请选择有效的信息类型和物品分类。')
                location = field('location', 2, 60)
                if location not in app.config['CAMPUS_LOCATIONS']:
                    raise ValueError('请选择列表中的校园地点。')
                location_detail = request.form.get('location_detail', '').strip()
                if len(location_detail) > 100:
                    raise ValueError('位置补充最多填写 100 个字符。')
                try:
                    happened = date.fromisoformat(request.form.get('happened_on', ''))
                except ValueError:
                    raise ValueError('请选择有效的遗失或拾取日期。')
                if happened > (now() + timedelta(hours=8)).date() or happened < date(2020, 1, 1):
                    raise ValueError('日期不能晚于今天，也不能早于 2020 年。')
                item = Item(owner_id=g.user.id, kind=kind, category=category, happened_on=happened,
                            title=field('title', 2, 60), location=location, location_detail=location_detail,
                            description=field('description', 10, 1500), contact=field('contact', 3, 120))
                image_name = save_image(request.files.get('image'))
                item.image_name = image_name
                db.session.add(item)
                db.session.commit()
                flash('信息已提交，管理员审核通过后会公开展示。', 'success')
                return redirect(url_for('detail', item_id=item.id))
            except ValueError as exc:
                if image_name:
                    (Path(app.config['UPLOAD_FOLDER']) / image_name).unlink(missing_ok=True)
                error = str(exc)
        return render_template('发布.html', error=error), 400 if error else 200

    @app.get('/items/<int:item_id>')
    def detail(item_id):
        item = db.get_or_404(Item, item_id)
        if not visible(item):
            abort(404)
        mine = None
        incoming = []
        if g.user:
            mine = db.session.scalar(select(Claim).where(Claim.item_id == item.id, Claim.claimant_id == g.user.id))
            if g.user.id == item.owner_id or g.user.is_admin:
                incoming = db.session.scalars(select(Claim).where(Claim.item_id == item.id).order_by(Claim.created_at.desc())).all()
        can_contact = g.user and (g.user.id == item.owner_id or g.user.is_admin or (mine and mine.status == 'accepted'))
        return render_template('详情.html', item=item, mine=mine, incoming=incoming, can_contact=can_contact)

    @app.get('/items/<int:item_id>/image')
    def item_image(item_id):
        item = db.get_or_404(Item, item_id)
        if not visible(item) or not item.image_name:
            abort(404)
        path = Path(app.config['UPLOAD_FOLDER']) / item.image_name
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype='image/jpeg')

    @app.post('/items/<int:item_id>/claim')
    @signed_in
    def claim(item_id):
        item = db.get_or_404(Item, item_id)
        if item.is_demo or item.review != 'approved' or item.resolved or item.owner_id == g.user.id:
            abort(403)
        try:
            recent = db.session.scalar(select(db.func.count()).select_from(Claim).where(Claim.claimant_id == g.user.id, Claim.created_at > now() - timedelta(days=1)))
            if recent >= 10:
                raise ValueError('今天提交的申请较多，请稍后再试。')
            db.session.add(Claim(item_id=item.id, claimant_id=g.user.id, proof=field('proof', 10, 1000), contact=field('contact', 3, 120)))
            db.session.commit()
            flash('已发送给发布者，核对通过后可查看双方联系方式。', 'success')
        except IntegrityError:
            db.session.rollback()
            flash('你已经为这件物品提交过申请，请在“我的记录”查看进度。', 'error')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('detail', item_id=item_id))

    @app.post('/claims/<int:claim_id>/decide')
    @signed_in
    def decide_claim(claim_id):
        application = db.get_or_404(Claim, claim_id)
        item = application.item
        if item.owner_id != g.user.id:
            abort(403)
        action = request.form.get('action')
        if action not in ('accept', 'reject'):
            abort(400)
        if application.status != 'pending' or item.resolved or item.review != 'approved':
            flash('这条记录的状态已变化，请刷新后查看。', 'error')
            return redirect(url_for('detail', item_id=item.id))
        if action == 'accept':
            # 条件更新避免两个并发确认同时选中不同认领者。
            changed = db.session.execute(update(Item).where(Item.id == item.id, Item.resolved.is_(False), Item.review == 'approved').values(resolved=True))
            if changed.rowcount != 1:
                db.session.rollback()
                abort(409)
            db.session.execute(update(Claim).where(Claim.item_id == item.id, Claim.status == 'pending').values(status='rejected'))
            application.status = 'accepted'
        else:
            application.status = 'rejected'
        db.session.commit()
        flash('已确认找回，双方现在可查看联系方式。' if action == 'accept' else '已标记为未通过。', 'success')
        return redirect(url_for('detail', item_id=item.id))

    @app.post('/items/<int:item_id>/close')
    @signed_in
    def close_item(item_id):
        item = db.get_or_404(Item, item_id)
        if item.owner_id != g.user.id:
            abort(403)
        action = request.form.get('action')
        if action not in ('resolved', 'withdraw'):
            abort(400)
        if action == 'resolved':
            if item.review != 'approved' or item.resolved:
                abort(409)
            item.resolved = True
        else:
            item.review = 'withdrawn'
        db.session.execute(update(Claim).where(Claim.item_id == item.id, Claim.status == 'pending').values(status='rejected'))
        db.session.commit()
        flash('物品状态已更新。', 'success')
        return redirect(url_for('dashboard'))

    @app.get('/me')
    @signed_in
    def dashboard():
        items = db.paginate(select(Item).where(Item.owner_id == g.user.id, Item.is_demo.is_(False)).order_by(Item.created_at.desc()), per_page=12, error_out=False)
        claims = db.session.scalars(select(Claim).join(Item).where(Claim.claimant_id == g.user.id, Item.is_demo.is_(False)).order_by(Claim.created_at.desc()).limit(50)).all()
        incoming = db.session.scalars(select(Claim).join(Item).where(Item.owner_id == g.user.id, Claim.status == 'pending', Item.is_demo.is_(False)).order_by(Claim.created_at.desc()).limit(50)).all()
        return render_template('我的记录.html', page=items, claims=claims, incoming=incoming)

    @app.get('/admin')
    @admin_only
    def admin():
        state = request.args.get('state', 'pending')
        if state not in REVIEW_NAMES:
            state = 'pending'
        page = db.paginate(select(Item).where(Item.review == state, Item.is_demo.is_(False)).order_by(Item.created_at.desc()), per_page=12, error_out=False)
        pending_count = db.session.scalar(select(db.func.count()).select_from(Item).where(Item.review == 'pending', Item.is_demo.is_(False)))
        return render_template('管理后台.html', page=page, state=state, pending_count=pending_count)

    @app.post('/admin/items/<int:item_id>')
    @admin_only
    def review_item(item_id):
        item = db.get_or_404(Item, item_id)
        action = request.form.get('action')
        if action not in ('approve', 'reject'):
            abort(400)
        if item.review == 'withdrawn':
            abort(409)
        note = request.form.get('note', '').strip()[:200]
        if action == 'reject' and not note:
            flash('未通过时请填写原因，方便发布者了解。', 'error')
            return redirect(url_for('admin'))
        item.review = 'approved' if action == 'approve' else 'rejected'
        item.review_note = note
        if action == 'reject':
            db.session.execute(update(Claim).where(Claim.item_id == item.id, Claim.status == 'pending').values(status='rejected'))
        db.session.commit()
        flash('审核结果已保存。', 'success')
        return redirect(url_for('admin'))

    @app.get('/about/<page>')
    def about(page):
        if page not in ('privacy', 'rules', 'help'):
            abort(404)
        return render_template('说明.html', page=page)

    @app.errorhandler(CSRFError)
    def csrf_error(_):
        return render_template('错误.html', code=400, message='表单已过期或校验失败。请返回页面、刷新后重新提交。'), 400

    for code, message in {400: '请求内容不正确，请返回重试。', 403: '你没有操作这条记录的权限。',
                          404: '这条信息不存在，或暂未公开。', 409: '记录状态已变化，请刷新后重试。',
                          413: '上传内容过大，单张图片请控制在 6 MB 内。',
                          429: '操作过于频繁，请稍后再试。', 500: '服务暂时遇到问题，请稍后重试。'}.items():
        def handler(error, code=code, message=message):
            db.session.rollback()
            return render_template('错误.html', code=code, message=message), code
        app.register_error_handler(code, handler)

    @app.cli.command('init-db')
    def init_db():
        """创建缺失的数据表，不删除已有数据。"""
        db.create_all()
        click.echo('数据库已准备好。请继续执行 create-admin 创建管理员。')

    @app.cli.command('create-admin')
    @click.option('--username', prompt='管理员昵称')
    @click.password_option(prompt='管理员密码（至少 10 个字符）', confirmation_prompt='再次输入密码')
    def create_admin(username, password):
        if not re.fullmatch(r'[\w\u4e00-\u9fff]{2,24}', username) or not 10 <= len(password) <= 128:
            raise click.ClickException('昵称需 2 至 24 个合法字符，密码需 10 至 128 个字符。')
        if db.session.scalar(select(User).where(User.username == username)):
            raise click.ClickException('昵称已存在，请选择新的管理员昵称。')
        db.session.add(User(username=username, password_hash=generate_password_hash(password), is_admin=True))
        db.session.commit()
        click.echo('管理员已创建，请在网站登录。')

    @app.cli.command('reset-password')
    @click.argument('username')
    @click.password_option(prompt='新密码', confirmation_prompt='再次输入密码')
    def reset_password(username, password):
        user = db.session.scalar(select(User).where(User.username == username))
        if not user or not 10 <= len(password) <= 128:
            raise click.ClickException('用户不存在，或密码长度不符合要求。')
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        click.echo('密码已重置。')

    @app.cli.command('block-user')
    @click.argument('username')
    @click.option('--unblock', is_flag=True, help='恢复账号')
    def block_user(username, unblock):
        user = db.session.scalar(select(User).where(User.username == username))
        if not user or user.is_admin:
            raise click.ClickException('用户不存在，或该账号为管理员。')
        user.blocked = not unblock
        db.session.commit()
        click.echo('账号状态已更新。')

    return app
