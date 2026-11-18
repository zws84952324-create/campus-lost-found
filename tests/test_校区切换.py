"""校区隔离、联动地点、登录延续和旧数据库升级检查。"""
import re
from urllib.parse import unquote, urlsplit, parse_qs

import pytest
from sqlalchemy import inspect, select

from test_业务流程 import app, client_as, post_item, valid_form, user_id
from app import db, User, Item, Claim


def test_campus_configuration_and_home_photo(app):
    assert len(app.config['CAMPUSES']['西校区']) == 14
    assert app.config['CAMPUSES']['东校区'] == ['食堂', '操场', '澡堂', '东停车场', '西停车场', '明德楼', '弘德楼', '西男生宿舍', '东男生宿舍', '女生宿舍', '西区小花坛']
    client = app.test_client()
    west = unquote(client.get('/?campus=西校区').get_data(as_text=True))
    east = unquote(client.get('/?campus=东校区').get_data(as_text=True))
    assert '图书馆' in west and '东停车场' not in west
    assert '西校区正门.webp' in west and '西校区正门.webp' not in east
    assert '东停车场' in east and '弘毅楼' not in east
    assert '保定职业技术学院（东校区）' in east
    assert '校园失物招领' in east


def test_campus_feed_counts_filters_and_pagination(app):
    post_item(app, title='西区操场物品', location='操场')
    post_item(app, title='西区已经找回', resolved=True)
    for i in range(13):
        post_item(app, title=f'东区操场物品{i}', campus='东校区', location='操场')
    post_item(app, title='东区退回物品', campus='东校区', location='食堂', review='rejected')
    client = app.test_client()
    html = client.get('/?campus=东校区&location=操场').get_data(as_text=True)
    assert '13 条符合条件' in html and '西区操场物品' not in html
    stats = re.findall(r'class="stat-value heading-5">(\d+)</span>', html)
    assert stats == ['0', '13', '0']
    next_link = re.search(r'href="([^"]+)">下一页', html).group(1).replace('&amp;', '&')
    assert parse_qs(urlsplit(next_link).query)['campus'] == ['东校区']
    assert '东区操场物品0' in client.get(next_link).get_data(as_text=True)
    west = client.get('/?campus=西校区&location=操场').get_data(as_text=True)
    assert '1 条符合条件' in west and '东区操场物品' not in west
    assert client.get('/?campus=不存在').status_code == 400


@pytest.mark.parametrize('campus,location', [('西校区', '图书馆'), ('东校区', '明德楼'), ('东校区', '西区小花坛')])
def test_publish_in_selected_campus(app, campus, location):
    owner = client_as(app, '发布同学')
    result = owner.post('/publish', data={**valid_form(), 'campus': campus, 'location': location})
    assert result.status_code == 302
    item_id = int(result.headers['Location'].rsplit('/', 1)[1])
    with app.app_context():
        item = db.session.get(Item, item_id)
        assert (item.campus, item.location) == (campus, location)
    html = owner.get(result.headers['Location']).get_data(as_text=True)
    assert f'class="title-3">{campus}</dd>' in html
    assert campus in owner.get('/me').get_data(as_text=True)
    with owner.session_transaction() as session:
        assert session['campus'] == campus


@pytest.mark.parametrize('campus,location', [('东校区', '弘毅楼'), ('西校区', '明德楼'), ('其他校区', '操场'), ('', '操场')])
def test_reject_mismatched_campus_and_location(app, campus, location):
    owner = client_as(app, '发布同学')
    bad = {**valid_form(), 'campus': campus, 'location': location}
    assert owner.post('/publish', data=bad).status_code == 400
    item_id = post_item(app)
    assert owner.post(f'/items/{item_id}/edit', data=bad).status_code == 400
    with app.app_context():
        item = db.session.get(Item, item_id)
        assert item.campus == '西校区' and item.location == '弘毅楼'


def test_edit_campus_requires_review_and_keeps_original_selection(app):
    item_id = post_item(app)
    owner = client_as(app, '发布同学')
    owner.get('/?campus=东校区')
    html = owner.get(f'/items/{item_id}/edit').get_data(as_text=True)
    assert '<option value="西校区" selected>' in html
    assert owner.post(f'/items/{item_id}/edit', data={**valid_form(), 'campus': '东校区', 'location': '弘德楼'}).status_code == 302
    with app.app_context():
        item = db.session.get(Item, item_id)
        assert item.campus == '东校区' and item.location == '弘德楼' and item.review == 'pending'
    assert app.test_client().get(f'/items/{item_id}').status_code == 404
    admin = client_as(app, '管理员')
    assert '东校区 · 弘德楼' in admin.get('/admin').get_data(as_text=True)
    admin.post(f'/admin/items/{item_id}', data={'action': 'approve'})
    assert '蓝色挂件钥匙' not in app.test_client().get('/?campus=西校区').get_data(as_text=True)
    assert valid_form()['title'] in app.test_client().get('/?campus=东校区').get_data(as_text=True)


def test_login_remembers_campus_and_returns_to_publish(app):
    client = app.test_client()
    client.get('/?campus=东校区')
    assert '东校区失物广场' in client.get('/').get_data(as_text=True)
    response = client.get('/publish?campus=东校区')
    next_path = parse_qs(urlsplit(response.headers['Location']).query)['next'][0]
    assert parse_qs(urlsplit(next_path).query)['campus'] == ['东校区']
    response = client.post('/login', data={'username': '发布同学', 'password': 'test-password-123', 'next': next_path})
    assert parse_qs(urlsplit(response.headers['Location']).query)['campus'] == ['东校区']
    assert '<option value="东校区" selected>' in client.get(response.headers['Location']).get_data(as_text=True)
    client.post('/logout')
    assert '东校区失物广场' in client.get('/').get_data(as_text=True)


def test_upgrade_old_database_preserves_users_items_claims_and_is_repeatable(app):
    item_id = post_item(app, image_name='existing-image.jpg')
    with app.app_context():
        owner = db.session.get(User, user_id(app, '发布同学'))
        original_hash = owner.password_hash
        db.session.add(Claim(item_id=item_id, claimant_id=user_id(app, '认领同学'), proof='旧申请说明', contact='旧联系方式'))
        db.session.commit()
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql('DROP INDEX ix_item_campus_feed')
            connection.exec_driver_sql('ALTER TABLE item DROP COLUMN campus')
    runner = app.test_cli_runner()
    for _ in range(2):
        result = runner.invoke(args=['init-db'])
        assert result.exit_code == 0, result.output
    with app.app_context():
        item = db.session.get(Item, item_id)
        assert item.campus == '西校区' and item.title == '蓝色挂件钥匙'
        assert item.image_name == 'existing-image.jpg'
        assert item.owner.password_hash == original_hash
        assert db.session.scalar(select(Claim)).proof == '旧申请说明'
        assert 'ix_item_campus_feed' in {index['name'] for index in inspect(db.engine).get_indexes('item')}
