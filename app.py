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
from urllib.parse import urlsplit
from uuid import uuid4

import click
from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import event, inspect, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
db = SQLAlchemy()
csrf = CSRFProtect()
CATEGORIES = ('证件卡片', '数码设备', '钥匙', '包袋衣物', '书本文具', '日用物品', '其他')
REVIEW_NAMES = {'pending': '待审核', 'approved': '已发布', 'rejected': '审核未通过', 'withdrawn': '已撤下'}
CLAIM_NAMES = {'pending': '等待核对', 'accepted': '等待交接', 'completed': '已交接',
               'rejected': '未通过', 'cancelled': '已取消', 'closed': '已结束'}


def now():
    # 数据库存 UTC，显示时统一转换为北京时间。
    return datetime.now(timezone.utc).replace(tzinfo=None)


def auth_stamp(user, secret):
    """密码变化后旧 Cookie 自动失效，不把密码哈希放进 Cookie。"""
    return hmac.new(secret.encode(), f'{user.id}:{user.password_hash}'.encode(), hashlib.sha256).hexdigest()


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
    campus = db.Column(db.String(20), nullable=False, default='西校区', server_default='西校区')
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
    __table_args__ = (db.Index('ix_item_feed', 'review', 'resolved', 'kind', 'created_at'),
                     db.Index('ix_item_campus_feed', 'campus', 'review', 'resolved', 'created_at'))


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


def initialize_database():
    """显式初始化或升级；旧物品归西校区，可重复执行，不重建表。"""
    db.create_all()
    with db.engine.begin() as connection:
        columns = {column['name'] for column in inspect(connection).get_columns('item')}
        if 'campus' not in columns:
            connection.exec_driver_sql("ALTER TABLE item ADD COLUMN campus VARCHAR(20) NOT NULL DEFAULT '西校区'")
        index = next(index for index in Item.__table__.indexes if index.name == 'ix_item_campus_feed')
        index.create(connection, checkfirst=True)


def signed_in(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            flash('请先登录，再继续操作。', 'info')
            return redirect(url_for('login', next=request.full_path.rstrip('?')))
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
        CAMPUSES=campus['校区地点'],
        DEFAULT_CAMPUS=campus['默认校区'],
    )
    if os.environ.get('ALLOWED_HOSTS'):
        app.config['TRUSTED_HOSTS'] = os.environ['ALLOWED_HOSTS'].split(',')
    if test_config:
        app.config.update(test_config)
    # 兼容旧 .env 中随首版复制的标题，避免东校区页面仍标为西校区。
    if app.config['SITE_NAME'] == '西校区失物招领':
        app.config['SITE_NAME'] = '校园失物招领'
    if app.config['SCHOOL_NAME'] == '保定职业技术学院（西校区）':
        app.config['SCHOOL_NAME'] = '保定职业技术学院'
    Path(app.config['UPLOAD_FOLDER']).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    csrf.init_app(app)

    @app.before_request
    def load_user():
        g.campus = session.get('campus', app.config['DEFAULT_CAMPUS'])
        if g.campus not in app.config['CAMPUSES']:
            g.campus = app.config['DEFAULT_CAMPUS']
        if request.endpoint in ('index', 'publish') and 'campus' in request.args:
            selected = request.args['campus']
            if selected not in app.config['CAMPUSES']:
                abort(400)
            g.campus = selected
            if request.method == 'GET':
                session['campus'] = selected
        g.user = db.session.get(User, session.get('user_id')) if session.get('user_id') else None
        if g.user and (g.user.blocked or not hmac.compare_digest(
                session.get('auth_stamp', ''), auth_stamp(g.user, app.secret_key))):
            session.clear()
            g.user = None

    @app.url_defaults
    def campus_urls(endpoint, values):
        if endpoint in ('index', 'publish'):
            values.setdefault('campus', getattr(g, 'campus', app.config['DEFAULT_CAMPUS']))

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
                    school_name=app.config['SCHOOL_NAME'], campuses=app.config['CAMPUSES'],
                    current_campus=getattr(g, 'campus', app.config['DEFAULT_CAMPUS']),
                    campus_locations=app.config['CAMPUSES'][getattr(g, 'campus', app.config['DEFAULT_CAMPUS'])])

    @app.template_filter('beijing')
    def beijing(value):
        return (value + timedelta(hours=8)).strftime('%m月%d日 %H:%M')

    @app.template_filter('claim_label')
    def claim_label(application):
        # 老版在核对通过时已标记找回，保留旧记录的已完成含义。
        if application.status == 'accepted' and application.item.resolved:
            return '已交接'
        return CLAIM_NAMES.get(application.status, '已结束')

    def begin_session(user):
        session.clear()
        session['campus'] = g.campus
        session['user_id'] = user.id
        session['auth_stamp'] = auth_stamp(user, app.secret_key)
        session.permanent = True

    def next_page():
        target = request.values.get('next', '')
        try:
            parsed = urlsplit(target)
        except ValueError:
            return url_for('dashboard')
        if (not target.startswith('/') or target.startswith('//') or parsed.scheme or parsed.netloc
                or '\\' in target or any(ord(char) < 32 for char in target)):
            return url_for('dashboard')
        try:
            endpoint, _ = app.url_map.bind('').match(parsed.path, method='GET')
        except Exception:
            return url_for('dashboard')
        if endpoint in ('login', 'register', 'static'):
            return url_for('dashboard')
        return target

    def lock_item(item_id):
        # 同一物品的状态操作先取得写锁；SQLite 和 MySQL 均使用同一顺序。
        # 不新增字段，更新旧版网站不需要改变表结构。
        db.session.execute(update(Item).where(Item.id == item_id).values(resolved=Item.resolved))
        db.session.expire_all()
        item = db.get_or_404(Item, item_id, populate_existing=True, with_for_update=True)
        if item.is_demo:
            abort(404)
        return item

    def accepted_claim(item_id, lock=False):
        query = select(Claim).where(Claim.item_id == item_id, Claim.status == 'accepted')
        return db.session.scalar(query.with_for_update() if lock else query)

    def close_applications(item_id, statuses=('pending', 'accepted')):
        db.session.execute(update(Claim).where(Claim.item_id == item_id, Claim.status.in_(statuses)).values(status='closed'))

    def complete_item(item):
        item.resolved = True
        db.session.execute(update(Claim).where(Claim.item_id == item.id, Claim.status == 'accepted').values(status='completed'))
        close_applications(item.id, ('pending',))

    def field(name, minimum=1, maximum=200):
        value = request.form.get(name, '').strip()
        if maximum is None:
            if not value:
                raise ValueError('请填写物品描述。')
        elif not minimum <= len(value) <= maximum:
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
        query = select(Item).where(Item.review == 'approved', Item.is_demo.is_(False), Item.campus == g.campus)
        if kind in ('lost', 'found'):
            query = query.where(Item.kind == kind)
        if category in CATEGORIES:
            query = query.where(Item.category == category)
        if location in app.config['CAMPUSES'][g.campus]:
            query = query.where(Item.location == location)
        else:
            location = ''
        if status == 'closed':
            query = query.where(Item.resolved.is_(True))
        elif status != 'all':
            query = query.where(Item.resolved.is_(False))
        if q:
            query = query.where(or_(Item.title.contains(q, autoescape=True), Item.location.contains(q, autoescape=True), Item.description.contains(q, autoescape=True)))
        page = db.paginate(query.options(joinedload(Item.owner)).order_by(Item.created_at.desc(), Item.id.desc()), per_page=12, error_out=False)
        handover_ids = set(db.session.scalars(select(Claim.item_id).where(Claim.item_id.in_([i.id for i in page.items]), Claim.status == 'accepted')))
        counts = dict(db.session.execute(select(Item.kind, db.func.count()).where(Item.review == 'approved', Item.resolved.is_(False), Item.is_demo.is_(False), Item.campus == g.campus).group_by(Item.kind)).all())
        returned = db.session.scalar(select(db.func.count()).select_from(Item).where(Item.review == 'approved', Item.resolved.is_(True), Item.is_demo.is_(False), Item.campus == g.campus))
        return render_template('首页.html', page=page, q=q, kind=kind, category=category, location=location, status=status, counts=counts, returned=returned, handover_ids=handover_ids)

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
                begin_session(user)
                flash('账号已创建，现在可以发布信息。', 'success')
                return redirect(next_page())
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
                begin_session(user)
                return redirect(next_page())
        return render_template('账户.html', mode='login', error=error), 400 if error else 200

    @app.post('/logout')
    def logout():
        session.clear()
        session['campus'] = g.campus
        return redirect(url_for('index'))

    @app.route('/me/password', methods=['GET', 'POST'])
    @signed_in
    def change_password():
        error = None
        if request.method == 'POST':
            rate_limit('change-password', 10, 15, identity=str(g.user.id))
            current = request.form.get('current_password', '')
            password = request.form.get('password', '')
            if len(current) > 128 or not check_password_hash(g.user.password_hash, current):
                error = '当前密码不正确，请重新输入。'
            elif not 10 <= len(password) <= 128:
                error = '新密码请使用 10 至 128 个字符。'
            elif password != request.form.get('confirm_password'):
                error = '两次输入的新密码不一致。'
            elif password == current:
                error = '新密码不能和当前密码相同。'
            else:
                g.user.password_hash = generate_password_hash(password)
                db.session.commit()
                begin_session(g.user)
                flash('密码已修改，其他设备需要重新登录。', 'success')
                return redirect(url_for('dashboard'))
        return render_template('修改密码.html', error=error), 400 if error else 200

    def item_fields():
        kind, category = field('kind'), field('category')
        selected_campus = field('campus', 1, 20)
        if selected_campus not in app.config['CAMPUSES']:
            raise ValueError('请选择有效的校区。')
        if kind not in ('lost', 'found') or category not in CATEGORIES:
            raise ValueError('请选择有效的信息类型和物品分类。')
        location = field('location', 2, 60)
        if location not in app.config['CAMPUSES'][selected_campus]:
            raise ValueError('请选择所属校区内的地点，切换校区后需要重新选择地点。')
        location_detail = request.form.get('location_detail', '').strip()
        if len(location_detail) > 100:
            raise ValueError('位置补充最多填写 100 个字符。')
        try:
            happened = date.fromisoformat(request.form.get('happened_on', ''))
        except ValueError:
            raise ValueError('请选择有效的遗失或拾取日期。')
        if happened > (now() + timedelta(hours=8)).date() or happened < date(2020, 1, 1):
            raise ValueError('日期不能晚于今天，也不能早于 2020 年。')
        return dict(kind=kind, category=category, campus=selected_campus, happened_on=happened, location=location,
                    location_detail=location_detail, title=field('title', 2, 60),
                    description=field('description', maximum=None), contact=field('contact', 3, 120))

    def remove_image(filename):
        if filename:
            try:
                (Path(app.config['UPLOAD_FOLDER']) / filename).unlink(missing_ok=True)
            except OSError:
                app.logger.warning('清理已失效图片失败，请维护者检查图片目录。')

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
                item = Item(owner_id=g.user.id, **item_fields())
                image_name = save_image(request.files.get('image'))
                item.image_name = image_name
                db.session.add(item)
                db.session.commit()
                session['campus'] = item.campus
                flash('信息已提交，管理员审核通过后会公开展示。', 'success')
                return redirect(url_for('detail', item_id=item.id))
            except ValueError as exc:
                db.session.rollback()
                remove_image(image_name)
                error = str(exc)
            except SQLAlchemyError:
                db.session.rollback()
                remove_image(image_name)
                app.logger.exception('保存发布信息失败')
                return render_template('发布.html', error='信息未保存，请稍后重新提交。', values=request.form, item=None), 503
        return render_template('发布.html', error=error, values=request.form, item=None), 400 if error else 200

    @app.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
    @signed_in
    def edit_item(item_id):
        item = db.get_or_404(Item, item_id)
        if item.owner_id != g.user.id or item.is_demo:
            abort(403)
        if request.method == 'POST':
            item = lock_item(item_id)
        if item.resolved or accepted_claim(item.id, lock=request.method == 'POST'):
            flash('物品已找回或正在交接，不能修改。请先取消尚未完成的交接。', 'info')
            return redirect(url_for('detail', item_id=item.id))
        error, new_image = None, None
        values = {name: getattr(item, name) for name in ('kind', 'category', 'title', 'campus', 'location', 'location_detail', 'description', 'contact')}
        values['happened_on'] = item.happened_on.isoformat()
        if request.method == 'POST':
            values = request.form
            try:
                fields = item_fields()
                new_image = save_image(request.files.get('image'))
                old_image = item.image_name
                if new_image:
                    item.image_name = new_image
                elif request.form.get('remove_image') == 'yes':
                    item.image_name = None
                for name, value in fields.items():
                    setattr(item, name, value)
                item.review, item.review_note = 'pending', ''
                close_applications(item.id)
                db.session.commit()
                session['campus'] = item.campus
                if old_image and old_image != item.image_name:
                    remove_image(old_image)
                flash('修改已保存，重新审核通过后公开。原有待核对申请已结束，对方可重新提交。', 'success')
                return redirect(url_for('detail', item_id=item.id))
            except ValueError as exc:
                db.session.rollback()
                remove_image(new_image)
                error = str(exc)
            except SQLAlchemyError:
                db.session.rollback()
                remove_image(new_image)
                app.logger.exception('保存修改失败')
                return render_template('发布.html', item=item, values=values, error='修改未保存，原有信息仍保留，请稍后重试。'), 503
        return render_template('发布.html', item=item, values=values, error=error), 400 if error else 200

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
                incoming = db.session.scalars(select(Claim).options(joinedload(Claim.claimant)).where(Claim.item_id == item.id).order_by(Claim.created_at.desc())).all()
        handover = accepted_claim(item.id) if not item.resolved else None
        can_contact = g.user and (g.user.id == item.owner_id or g.user.is_admin or
                                 (mine and mine.status in ('accepted', 'completed') and item.review == 'approved'))
        return render_template('详情.html', item=item, mine=mine, incoming=incoming,
                               can_contact=can_contact, handover=handover)

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
        rate_limit('claim-submit', 10, 24 * 60, identity=str(g.user.id))
        item = lock_item(item_id)
        if item.review != 'approved' or item.resolved or item.owner_id == g.user.id:
            abort(403)
        if accepted_claim(item.id, lock=True):
            flash('这件物品正在交接，暂时不接收新申请。', 'info')
            return redirect(url_for('detail', item_id=item.id))
        try:
            recent = db.session.scalar(select(db.func.count()).select_from(Claim).where(Claim.claimant_id == g.user.id, Claim.created_at > now() - timedelta(days=1)))
            if recent >= 10:
                raise ValueError('今天提交的申请较多，请稍后再试。')
            previous = db.session.scalar(select(Claim).where(Claim.item_id == item.id, Claim.claimant_id == g.user.id).with_for_update())
            if previous and previous.status not in ('cancelled', 'closed'):
                flash('你已经提交过申请，请在“我的记录”查看进度。', 'info')
                return redirect(url_for('detail', item_id=item.id))
            proof, contact = field('proof', 10, 1000), field('contact', 3, 120)
            if previous:
                previous.proof, previous.contact = proof, contact
                previous.status, previous.created_at = 'pending', now()
            else:
                db.session.add(Claim(item_id=item.id, claimant_id=g.user.id, proof=proof, contact=contact))
            db.session.commit()
            flash('已发送给发布者，核对通过后可查看双方联系方式。', 'success')
        except IntegrityError:
            db.session.rollback()
            flash('你已经为这件物品提交过申请，请在“我的记录”查看进度。', 'error')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        return redirect(url_for('detail', item_id=item_id))

    @app.post('/claims/<int:claim_id>/decide')
    @signed_in
    def decide_claim(claim_id):
        application = db.get_or_404(Claim, claim_id)
        item = lock_item(application.item_id)
        db.session.refresh(application, with_for_update=True)
        if item.owner_id != g.user.id:
            abort(403)
        action = request.form.get('action')
        if action not in ('accept', 'reject'):
            abort(400)
        if application.status != 'pending' or item.resolved or item.review != 'approved':
            flash('这条记录的状态已变化，请刷新后查看。', 'error')
            return redirect(url_for('detail', item_id=item.id))
        if action == 'accept':
            if accepted_claim(item.id, lock=True):
                abort(409)
            if application.claimant.blocked:
                flash('该申请人的账号已被限制，请核对其他申请。', 'error')
                return redirect(url_for('detail', item_id=item.id))
            application.status = 'accepted'
        else:
            application.status = 'rejected'
        db.session.commit()
        flash('核对通过，双方现在可查看联系方式。实际交接后再确认找回。' if action == 'accept' else '已标记为未通过。', 'success')
        return redirect(url_for('detail', item_id=item.id))

    @app.post('/claims/<int:claim_id>/cancel')
    @signed_in
    def cancel_claim(claim_id):
        application = db.get_or_404(Claim, claim_id)
        item = lock_item(application.item_id)
        db.session.refresh(application, with_for_update=True)
        if g.user.id not in (application.claimant_id, item.owner_id):
            abort(403)
        if application.status not in ('pending', 'accepted') or item.resolved:
            abort(409)
        application.status = 'cancelled'
        db.session.commit()
        flash('申请已取消，双方不再通过这次申请查看联系方式。', 'success')
        return redirect(url_for('detail', item_id=item.id) if visible(item) else url_for('dashboard'))

    @app.post('/claims/<int:claim_id>/complete')
    @signed_in
    def complete_claim(claim_id):
        application = db.get_or_404(Claim, claim_id)
        item = lock_item(application.item_id)
        db.session.refresh(application, with_for_update=True)
        if g.user.id not in (application.claimant_id, item.owner_id):
            abort(403)
        if item.review != 'approved' or item.resolved or application.status != 'accepted':
            abort(409)
        complete_item(item)
        db.session.commit()
        flash('已确认交接完成，物品现在计入“已找回”。', 'success')
        return redirect(url_for('detail', item_id=item.id))

    @app.post('/items/<int:item_id>/close')
    @signed_in
    def close_item(item_id):
        item = lock_item(item_id)
        if item.owner_id != g.user.id:
            abort(403)
        action = request.form.get('action')
        if action not in ('resolved', 'withdraw'):
            abort(400)
        if action == 'resolved':
            if item.review != 'approved' or item.resolved:
                abort(409)
            complete_item(item)
        else:
            item.review = 'withdrawn'
            close_applications(item.id)
        db.session.commit()
        flash('物品状态已更新。', 'success')
        return redirect(url_for('dashboard'))

    @app.get('/me')
    @signed_in
    def dashboard():
        items = db.paginate(select(Item).where(Item.owner_id == g.user.id, Item.is_demo.is_(False)).order_by(Item.created_at.desc()), per_page=12, error_out=False)
        claim_page = db.paginate(select(Claim).join(Item).options(joinedload(Claim.item)).where(Claim.claimant_id == g.user.id, Item.is_demo.is_(False)).order_by(Claim.created_at.desc(), Claim.id.desc()), page=request.args.get('claims_page', 1, type=int), per_page=12, error_out=False)
        incoming_page = db.paginate(select(Claim).join(Item).options(joinedload(Claim.item), joinedload(Claim.claimant)).where(Item.owner_id == g.user.id, Claim.status.in_(('pending', 'accepted')), Item.review == 'approved', Item.resolved.is_(False), Item.is_demo.is_(False)).order_by(Claim.created_at.desc(), Claim.id.desc()), page=request.args.get('incoming_page', 1, type=int), per_page=12, error_out=False)
        handover_ids = set(db.session.scalars(select(Claim.item_id).where(Claim.item_id.in_([i.id for i in items.items]), Claim.status == 'accepted')))
        return render_template('我的记录.html', page=items, claim_page=claim_page,
                               incoming_page=incoming_page, handover_ids=handover_ids)

    @app.get('/admin')
    @admin_only
    def admin():
        state = request.args.get('state', 'pending')
        if state not in REVIEW_NAMES:
            state = 'pending'
        page = db.paginate(select(Item).options(joinedload(Item.owner)).where(Item.review == state, Item.is_demo.is_(False)).order_by(Item.created_at.desc()), per_page=12, error_out=False)
        pending_count = db.session.scalar(select(db.func.count()).select_from(Item).where(Item.review == 'pending', Item.is_demo.is_(False)))
        return render_template('管理后台.html', page=page, state=state, pending_count=pending_count)

    @app.post('/admin/items/<int:item_id>')
    @admin_only
    def review_item(item_id):
        item = lock_item(item_id)
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
            close_applications(item.id)
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
        """创建或升级数据表，保留已有数据，旧信息归入西校区。"""
        initialize_database()
        click.echo('数据库已准备好，旧信息归入西校区。新安装请执行 create-admin 创建管理员。')

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
