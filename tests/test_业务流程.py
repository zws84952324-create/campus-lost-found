"""业务和权限回归测试，使用独立临时数据库，不触碰实际网站数据。"""
import io
import re
import sys
from datetime import date
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, db, User, Item, Claim, Attempt, auth_stamp


@pytest.fixture
def app(tmp_path):
    app = create_app({'TESTING': True, 'SECRET_KEY': 'tests-only-secret',
                      'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + (tmp_path / '测试.db').as_posix(),
                      'UPLOAD_FOLDER': str(tmp_path / '图片'), 'WTF_CSRF_ENABLED': False,
                      'SESSION_COOKIE_SECURE': False})
    with app.app_context():
        db.create_all()
        for name in ('发布同学', '认领同学', '其他同学', '管理员'):
            db.session.add(User(username=name, password_hash=generate_password_hash('test-password-123'), is_admin=name == '管理员'))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def user_id(app, name):
    with app.app_context():
        return db.session.scalar(select(User.id).where(User.username == name))


def client_as(app, name):
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = user_id(app, name)
        with app.app_context():
            session['auth_stamp'] = auth_stamp(db.session.get(User, session['user_id']), app.secret_key)
    return client


def post_item(app, review='approved', **overrides):
    with app.app_context():
        item = Item(owner_id=user_id(app, '发布同学'), kind='found', title='蓝色挂件钥匙',
                    category='钥匙', location='弘毅楼', happened_on=date(2026, 11, 10),
                    description='在一楼门口捡到一串钥匙，请说明独有特征。', contact='微信：owner-private-contact', review=review)
        for key, value in overrides.items():
            setattr(item, key, value)
        db.session.add(item)
        db.session.commit()
        return item.id


def valid_form():
    return dict(campus='西校区', kind='found', title='带蓝色挂件的一串钥匙', category='钥匙', location='弘毅楼',
                location_detail='一楼入口', happened_on='2026-11-10',
                description='在入口捡到钥匙，请说明钥匙数量与挂件背面的特征。', contact='微信：owner-private-contact')


def test_public_pages_and_campus_locations(app):
    client = app.test_client()
    for path in ('/', '/login', '/register', '/about/help', '/about/privacy', '/about/rules'):
        response = client.get(path)
        assert response.status_code == 200
    html = client.get('/').get_data(as_text=True)
    assert '保定职业技术学院（西校区）' in html
    assert all(place in html for place in app.config['CAMPUSES']['西校区'])
    assert client.get('/不存在').status_code == 404
    assert client.get('/publish').status_code == 302
    assert client_as(app, '其他同学').get('/admin').status_code == 403


def test_registration_login_and_duplicate(app):
    client = app.test_client()
    form = dict(username='新同学', password='long-secret-123', confirm_password='long-secret-123', agree='yes')
    assert client.post('/register', data=form).status_code == 302
    assert '新同学' in client.get('/me').get_data(as_text=True)
    assert client.post('/logout').status_code == 302
    assert client.post('/register', data=form).status_code == 400
    assert client.post('/login', data={'username':'新同学','password':'wrong'}).status_code == 400
    assert client.post('/login', data={'username':'新同学','password':'long-secret-123'}).status_code == 302
    with app.app_context():
        saved = db.session.scalar(select(User).where(User.username == '新同学'))
        assert saved.password_hash != form['password']


def test_publish_review_claim_full_flow_and_privacy(app):
    owner, claimant, admin = [client_as(app, name) for name in ('发布同学', '认领同学', '管理员')]
    response = owner.post('/publish', data=valid_form())
    assert response.status_code == 302
    location = response.headers['Location']
    item_id = int(location.rsplit('/', 1)[-1])
    assert app.test_client().get(location).status_code == 404
    assert owner.get(location).status_code == 200
    assert admin.get('/admin').status_code == 200
    assert admin.post(f'/admin/items/{item_id}', data={'action':'approve'}).status_code == 302
    public = app.test_client().get(location).get_data(as_text=True)
    assert 'owner-private-contact' not in public
    assert '带蓝色挂件的一串钥匙' in public
    claim_form = {'proof':'private-proof 挂件背面有三颗星星。', 'contact':'微信：claimant-private-contact'}
    assert claimant.post(location + '/claim', data=claim_form).status_code == 302
    assert 'private-proof' not in app.test_client().get(location).get_data(as_text=True)
    assert 'private-proof' in owner.get(location).get_data(as_text=True)
    assert 'claimant-private-contact' not in owner.get(location).get_data(as_text=True)
    assert 'owner-private-contact' not in claimant.get(location).get_data(as_text=True)
    with app.app_context():
        application_id = db.session.scalar(select(Claim.id))
    assert claimant.post(f'/claims/{application_id}/decide', data={'action':'accept'}).status_code == 403
    assert owner.post(f'/claims/{application_id}/decide', data={'action':'accept'}).status_code == 302
    assert 'owner-private-contact' in claimant.get(location).get_data(as_text=True)
    assert 'claimant-private-contact' in owner.get(location).get_data(as_text=True)
    assert 'owner-private-contact' not in app.test_client().get(location).get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Item, item_id).resolved is False
        assert db.session.get(Claim, application_id).status == 'accepted'
    assert claimant.post(f'/claims/{application_id}/complete').status_code == 302
    with app.app_context():
        assert db.session.get(Item, item_id).resolved is True
        assert db.session.get(Claim, application_id).status == 'completed'


def test_invalid_fields_and_xss(app):
    owner = client_as(app, '发布同学')
    form = valid_form()
    for field, value in [('location','不存在的楼'),('kind','bad'),('happened_on','2099-01-01'),('description','   ')]:
        bad = {**form, field:value}
        assert owner.post('/publish', data=bad).status_code == 400
    item_id = post_item(app, title='<script>alert(1)</script>')
    html = app.test_client().get(f'/items/{item_id}').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_image_validation_and_private_access(app):
    owner = client_as(app, '发布同学')
    form = valid_form()
    form['image'] = (io.BytesIO(b'<script>evil</script>'), 'fake.jpg')
    assert owner.post('/publish', data=form, content_type='multipart/form-data').status_code == 400
    stream = io.BytesIO()
    Image.new('RGB', (1800, 1000), '#357e66').save(stream, 'PNG')
    stream.seek(0)
    form = {**valid_form(), 'image': (stream, '照片.png')}
    response = owner.post('/publish', data=form, content_type='multipart/form-data')
    assert response.status_code == 302
    item_id = int(response.headers['Location'].rsplit('/',1)[-1])
    assert app.test_client().get(f'/items/{item_id}/image').status_code == 404
    picture = owner.get(f'/items/{item_id}/image')
    assert picture.status_code == 200
    assert picture.mimetype == 'image/jpeg'
    with Image.open(io.BytesIO(picture.data)) as resized:
        assert max(resized.size) <= 1280
        assert not resized.getexif()
    assert 'no-store' in picture.headers['Cache-Control']


def test_csrf_is_required_on_all_writes(app):
    app.config['WTF_CSRF_ENABLED'] = True
    client = app.test_client()
    assert client.post('/login', data={'username':'发布同学','password':'test-password-123'}).status_code == 400
    html = client.get('/login').get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    assert client.post('/login', data={'username':'发布同学','password':'test-password-123','csrf_token':token}).status_code == 302
    assert client.post('/logout').status_code == 400


def test_filters_pagination_and_sql_special_characters(app):
    for i in range(14):
        post_item(app, title=f'钥匙记录{i}', location='弘毅楼' if i < 13 else '笃学楼')
    client = app.test_client()
    html = client.get('/?location=弘毅楼').get_data(as_text=True)
    assert '13 条符合条件' in html and '下一页' in html
    html2 = client.get('/?location=弘毅楼&page=2').get_data(as_text=True)
    assert '钥匙记录0' in html2
    assert '1 条符合条件' in client.get('/?location=笃学楼').get_data(as_text=True)
    assert '0 条符合条件' in client.get('/?q=%25').get_data(as_text=True)
    assert client.get('/?q=%27%20OR%201%3D1').status_code == 200


def test_ownership_duplicate_and_close(app):
    item_id = post_item(app)
    owner, claimant, other = [client_as(app, name) for name in ('发布同学','认领同学','其他同学')]
    claim_data = {'proof':'挂件后面刻了三颗星，请核对。','contact':'private-contact'}
    assert owner.post(f'/items/{item_id}/claim', data=claim_data).status_code == 403
    assert other.post(f'/items/{item_id}/close', data={'action':'resolved'}).status_code == 403
    claimant.post(f'/items/{item_id}/claim', data=claim_data)
    claimant.post(f'/items/{item_id}/claim', data=claim_data)
    with app.app_context():
        assert db.session.scalar(select(db.func.count()).select_from(Claim)) == 1
    assert owner.post(f'/items/{item_id}/close', data={'action':'withdraw'}).status_code == 302
    assert app.test_client().get(f'/items/{item_id}').status_code == 404
    assert claimant.get('/me').status_code == 200
    with app.app_context():
        assert db.session.scalar(select(Claim)).status == 'closed'


def test_accept_only_one_claim(app):
    item_id = post_item(app)
    for name in ('认领同学','其他同学'):
        client_as(app,name).post(f'/items/{item_id}/claim', data={'proof':'钥匙挂件的背面是星星图案。','contact':'微信联系测试'})
    with app.app_context():
        ids = db.session.scalars(select(Claim.id).order_by(Claim.id)).all()
    owner = client_as(app,'发布同学')
    owner.post(f'/claims/{ids[0]}/decide', data={'action':'accept'})
    owner.post(f'/claims/{ids[1]}/decide', data={'action':'accept'})
    with app.app_context():
        assert [db.session.get(Claim,i).status for i in ids] == ['accepted','pending']


def test_rate_limit_login_and_publication_quota(app):
    client = app.test_client()
    for _ in range(12):
        assert client.post('/login',data={'username':'wrong','password':'wrong'}).status_code == 400
    assert client.post('/login',data={'username':'wrong','password':'wrong'}).status_code == 429
    owner = client_as(app,'发布同学')
    for _ in range(5):
        assert owner.post('/publish',data=valid_form()).status_code == 302
    assert owner.post('/publish',data=valid_form()).status_code == 400


def test_admin_rejection_and_user_block(app):
    item_id = post_item(app,review='pending')
    admin = client_as(app,'管理员')
    admin.post(f'/admin/items/{item_id}',data={'action':'reject','note':'请遮挡证件号码'})
    with app.app_context():
        assert db.session.get(Item,item_id).review == 'rejected'
    owner = client_as(app,'发布同学')
    assert '请遮挡证件号码' in owner.get('/me').get_data(as_text=True)
    result = app.test_cli_runner().invoke(args=['block-user','发布同学'])
    assert result.exit_code == 0
    assert owner.get('/me').status_code == 302


def test_cli_and_headers(app):
    runner = app.test_cli_runner()
    assert runner.invoke(args=['init-db']).exit_code == 0
    assert runner.invoke(args=['seed-demo']).exit_code != 0
    response = app.test_client().get('/')
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert "script-src 'self'" in response.headers['Content-Security-Policy']
    assert 'no-store' in response.headers['Cache-Control']


def test_legacy_demo_stays_hidden(app):
    item_id = post_item(app, is_demo=True)
    client = app.test_client()
    html = client.get('/').get_data(as_text=True)
    assert '蓝色挂件钥匙' not in html
    assert '0 条符合条件' in html
    assert '本地演示环境' not in html
    assert client.get(f'/items/{item_id}').status_code == 404
    assert client_as(app, '发布同学').get(f'/items/{item_id}').status_code == 404
    claimant = client_as(app, '认领同学')
    assert claimant.post(f'/items/{item_id}/claim', data={'proof':'这串钥匙是我的，有星星挂件。','contact':'测试联系'}).status_code == 404
    assert '蓝色挂件钥匙' not in client_as(app, '管理员').get('/admin?state=approved').get_data(as_text=True)
