"""真实交接、编辑、账号会话和首页图片的回归检查。"""
import io
import re
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from test_业务流程 import app, client_as, post_item, valid_form, user_id
from app import db, Item, Claim


def apply_claim(app, item_id, name='认领同学'):
    client = client_as(app, name)
    response = client.post(f'/items/{item_id}/claim', data={'proof': '背面有三颗星星，这是核对说明。', 'contact': 'claim-private-contact'})
    assert response.status_code == 302
    with app.app_context():
        return db.session.scalar(select(Claim.id).where(Claim.item_id == item_id, Claim.claimant_id == user_id(app, name)))


@pytest.mark.parametrize('description', ['短', '这是完整的描述。' * 1000])
def test_description_has_no_character_validation(app, description):
    owner = client_as(app, '发布同学')
    response = owner.post('/publish', data={**valid_form(), 'description': description})
    assert response.status_code == 302
    location = response.headers['Location']
    item_id = int(location.rsplit('/', 1)[1])
    assert owner.post(location + '/edit', data={**valid_form(), 'description': description + '补充'}).status_code == 302
    with app.app_context():
        assert db.session.get(Item, item_id).description == description + '补充'
    for path in ('/publish', location + '/edit'):
        html = owner.get(path).get_data(as_text=True)
        tag = re.search(r'<textarea\b[^>]*name="description"[^>]*>', html).group()
        assert 'minlength' not in tag and 'maxlength' not in tag
        assert 'required' in tag


def test_edit_permissions_review_and_existing_claims(app):
    item_id = post_item(app)
    cid = apply_claim(app, item_id)
    url = f'/items/{item_id}'
    owner, other = client_as(app, '发布同学'), client_as(app, '其他同学')
    assert other.get(url + '/edit').status_code == 403
    assert other.post(url + '/edit', data=valid_form()).status_code == 403
    assert '蓝色挂件钥匙' in owner.get(url + '/edit').get_data(as_text=True)
    assert owner.post(url + '/edit', data={**valid_form(), 'description': ''}).status_code == 400
    with app.app_context():
        assert db.session.get(Item, item_id).review == 'approved'
        assert db.session.get(Claim, cid).status == 'pending'
    assert owner.post(url + '/edit', data={**valid_form(), 'description': '黑'}).status_code == 302
    assert other.get(url).status_code == 404
    with app.app_context():
        assert db.session.get(Item, item_id).review == 'pending'
        assert db.session.get(Claim, cid).status == 'closed'
    client_as(app, '管理员').post(f'/admin/items/{item_id}', data={'action': 'approve'})
    assert apply_claim(app, item_id) == cid


def image_form():
    stream = io.BytesIO()
    Image.new('RGB', (200, 120), '#26754a').save(stream, 'PNG')
    stream.seek(0)
    return {**valid_form(), 'image': (stream, '照片.png')}


def test_edit_image_lifecycle_and_rollback(app, monkeypatch):
    owner = client_as(app, '发布同学')
    response = owner.post('/publish', data=image_form())
    url = response.headers['Location']
    folder = Path(app.config['UPLOAD_FOLDER'])
    original = next(folder.glob('*.jpg'))
    assert owner.post(url + '/edit', data=valid_form()).status_code == 302
    assert original.exists()
    assert owner.post(url + '/edit', data={**valid_form(), 'image': (io.BytesIO(b'broken'), '坏图.png')}).status_code == 400
    assert original.exists() and len(list(folder.glob('*.jpg'))) == 1
    with monkeypatch.context() as patch:
        def fail_commit():
            raise SQLAlchemyError('test rollback')
        patch.setattr(db.session, 'commit', fail_commit)
        assert owner.post(url + '/edit', data=image_form()).status_code == 503
    assert original.exists() and len(list(folder.glob('*.jpg'))) == 1
    assert owner.post(url + '/edit', data=image_form()).status_code == 302
    assert not original.exists() and len(list(folder.glob('*.jpg'))) == 1
    assert owner.post(url + '/edit', data={**valid_form(), 'remove_image': 'yes'}).status_code == 302
    assert list(folder.glob('*.jpg')) == []


def test_cancel_handover_reapply_and_privacy(app):
    item_id = post_item(app)
    first, second = apply_claim(app, item_id), apply_claim(app, item_id, '其他同学')
    owner, claimant, other = [client_as(app, n) for n in ('发布同学', '认领同学', '其他同学')]
    url = f'/items/{item_id}'
    owner.post(f'/claims/{first}/decide', data={'action': 'accept'})
    assert '等待交接' in app.test_client().get(url).get_data(as_text=True)
    assert owner.post(url + '/edit', data=valid_form()).status_code == 302
    with app.app_context():
        assert db.session.get(Item, item_id).review == 'approved'
    assert other.post(f'/claims/{first}/cancel').status_code == 403
    assert other.post(f'/claims/{first}/complete').status_code == 403
    assert claimant.post(f'/claims/{first}/cancel').status_code == 302
    assert 'owner-private-contact' not in claimant.get(url).get_data(as_text=True)
    assert 'claim-private-contact' not in owner.get(url).get_data(as_text=True)
    assert claimant.post(f'/claims/{first}/complete').status_code == 409
    assert apply_claim(app, item_id) == first
    assert owner.post(f'/claims/{second}/decide', data={'action': 'accept'}).status_code == 302
    assert other.post(f'/claims/{second}/complete').status_code == 302
    assert owner.post(f'/claims/{second}/complete').status_code == 409
    with app.app_context():
        assert db.session.get(Claim, first).status == 'closed'
        assert db.session.get(Claim, second).status == 'completed'
        assert db.session.get(Item, item_id).resolved


@pytest.mark.parametrize('operation', ['withdraw', 'reject'])
def test_unpublish_revokes_contacts_and_handover(app, operation):
    item_id = post_item(app)
    cid = apply_claim(app, item_id)
    owner = client_as(app, '发布同学')
    claimant = client_as(app, '认领同学')
    owner.post(f'/claims/{cid}/decide', data={'action': 'accept'})
    if operation == 'withdraw':
        owner.post(f'/items/{item_id}/close', data={'action': 'withdraw'})
    else:
        client_as(app, '管理员').post(f'/admin/items/{item_id}', data={'action': 'reject', 'note': '信息需重新核对'})
    assert 'owner-private-contact' not in claimant.get('/me').get_data(as_text=True)
    assert claimant.post(f'/claims/{cid}/complete').status_code == 409
    with app.app_context():
        assert db.session.get(Claim, cid).status == 'closed'
    if operation == 'reject':
        client_as(app, '管理员').post(f'/admin/items/{item_id}', data={'action': 'approve'})
        assert 'owner-private-contact' not in claimant.get(f'/items/{item_id}').get_data(as_text=True)


def test_password_change_and_reset_expire_old_sessions(app):
    owner, old = client_as(app, '发布同学'), client_as(app, '发布同学')
    assert owner.get('/me/password').status_code == 200
    form = {'current_password': 'wrong', 'password': 'new-password-123', 'confirm_password': 'new-password-123'}
    assert owner.post('/me/password', data=form).status_code == 400
    assert old.get('/me').status_code == 200
    form['current_password'] = 'test-password-123'
    assert owner.post('/me/password', data={**form, 'confirm_password': 'different'}).status_code == 400
    assert owner.post('/me/password', data=form).status_code == 302
    assert owner.get('/me').status_code == 200
    assert old.get('/me').status_code == 302
    assert old.post('/login', data={'username': '发布同学', 'password': 'test-password-123'}).status_code == 400
    assert old.post('/login', data={'username': '发布同学', 'password': form['password']}).status_code == 302
    result = app.test_cli_runner().invoke(args=['reset-password', '发布同学', '--password', 'reset-password-123'])
    assert result.exit_code == 0
    assert old.get('/me').status_code == owner.get('/me').status_code == 302


@pytest.mark.parametrize('target', ['https://outside.example/', '//outside.example/', '/\\outside.example/', 'http://[', '/login', '/missing', '/%2f%2foutside.example'])
def test_login_next_rejects_unsafe_or_invalid_paths(app, target):
    response = app.test_client().post('/login', data={'username': '发布同学', 'password': 'test-password-123', 'next': target})
    assert response.headers['Location'] == '/me'


def test_login_next_returns_to_item_and_register_preserves_it(app):
    item_id = post_item(app)
    url = f'/items/{item_id}'
    client = app.test_client()
    assert f'next={url}' in client.get('/login', query_string={'next': url}).get_data(as_text=True)
    response = client.post('/login', data={'username': '发布同学', 'password': 'test-password-123', 'next': url})
    assert response.headers['Location'] == url
    client.post('/logout')
    response = client.post('/register', data={'username': '跳转同学', 'password': 'test-password-123', 'confirm_password': 'test-password-123', 'agree': 'yes', 'next': '/publish'})
    assert response.headers['Location'] == '/publish'


def test_dashboard_paginates_old_claims_without_truncation(app):
    with app.app_context():
        for i in range(53):
            item_id = post_item(app, title=f'历史线索{i}')
            db.session.add(Claim(item_id=item_id, claimant_id=user_id(app, '认领同学'), proof='私下核对说明', contact='private-contact'))
        db.session.commit()
    claimant, owner = client_as(app, '认领同学'), client_as(app, '发布同学')
    assert 'claims_page=2' in claimant.get('/me').get_data(as_text=True)
    assert '历史线索0' in claimant.get('/me?claims_page=5').get_data(as_text=True)
    html = owner.get('/me?page=2&incoming_page=2').get_data(as_text=True)
    assert 'incoming_page=3' in html and 'page=3' in html
    assert owner.get('/me?incoming_page=5').status_code == 200


def test_competing_approvals_accept_only_one(app):
    item_id = post_item(app)
    ids = [apply_claim(app, item_id, name) for name in ('认领同学', '其他同学')]
    clients = [client_as(app, '发布同学') for _ in ids]
    barrier = Barrier(2)
    def accept(pair):
        cid, client = pair
        barrier.wait(timeout=10)
        return client.post(f'/claims/{cid}/decide', data={'action': 'accept'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(accept, zip(ids, clients)))
    assert sorted(statuses) == [302, 409]
    with app.app_context():
        states = list(db.session.scalars(select(Claim.status)))
        assert sorted(states) == ['accepted', 'pending']
        assert not db.session.get(Item, item_id).resolved


def test_home_photo_and_legacy_complete_status(app):
    client = app.test_client()
    html = client.get('/').get_data(as_text=True)
    assert '西校区正门.webp' in unquote(html)
    assert '<option value="lost"' in html
    response = client.get('/static/西校区正门.webp')
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.data)) as img:
        assert img.size == (1190, 543)
    item_id = post_item(app)
    cid = apply_claim(app, item_id)
    with app.app_context():
        db.session.get(Item, item_id).resolved = True
        db.session.get(Claim, cid).status = 'accepted'
        db.session.commit()
    assert '已交接' in client_as(app, '认领同学').get('/me').get_data(as_text=True)
