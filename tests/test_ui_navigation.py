from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from test_planning_dashboard import client
from app import ui_pages


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.urls = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for key, value in attrs if key in {'href', 'src'})


def test_every_page_versions_navigation_assets_and_returns_no_store(client):
    version = client.get('/api/ui-version').json()['version']
    for route in sorted(ui_pages.PAGE_PATHS):
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers['X-Workspace-Version'] == version
        assert 'no-store' in response.headers['cache-control']
        assert f'<meta name="workspace-version" content="{version}">' in response.text
        urls = Links(response.text).urls
        page_links = [url for url in urls if urlsplit(url).path in ui_pages.PAGE_PATHS]
        assert len(page_links) >= 2
        for url in page_links:
            assert parse_qs(urlsplit(url).query)['ui'] == [version]
        for url in urls:
            if urlsplit(url).path.startswith('/static/'):
                assert parse_qs(urlsplit(url).query)['v'] == [version]
        # The master module intentionally exposes only its own sidebar. Return
        # through the top-level analysis module before opening reconciliation.
        analysis = client.get(next(url for url in page_links if urlsplit(url).path == '/'))
        returned = client.get(next(url for url in Links(analysis.text).urls if urlsplit(url).path == '/purchase-reconciliation'))
        assert 'id="reconcileKeyword"' in returned.text
        assert '物料仓库看板' not in returned.text


def test_version_links_preserve_hash_query_and_leave_downloads_untouched(client):
    assert ui_pages.versioned_url('/master-data?x=1&ui=old#mapping', 'new') == '/master-data?x=1&amp;ui=new#mapping'
    for url in ['/api/templates', '/api/master-data/materials/template?format=csv', '#upload', 'https://example.com/']:
        assert ui_pages.versioned_url(url, 'new') == url
    html = client.get('/data-import').text
    assert f'/master-data?ui={ui_pages.ui_version()}#mapping' in html
    assert client.get('/api/ui-version').headers['cache-control'] == 'no-store'


def test_ui_version_changes_when_asset_content_changes(tmp_path, monkeypatch):
    # Equal file size must not hide an in-place deployment update.
    (tmp_path / 'page.html').write_text('old', encoding='utf-8')
    monkeypatch.setattr(ui_pages, 'STATIC_DIR', tmp_path)
    old = ui_pages.ui_version()
    (tmp_path / 'page.html').write_text('new', encoding='utf-8')
    assert ui_pages.ui_version() != old


def test_two_modules_have_distinct_sidebars_and_active_switch(client):
    import re
    for route in sorted(ui_pages.PAGE_PATHS):
        page = client.get(route).text
        switch = page.split('class="module-switch"')[1].split('</div>')[0]
        assert '>看板分析</a>' in switch and '>基础数据</a>' in switch
        assert switch.count('aria-current="page"') == 1
        expected = '基础数据' if route == '/master-data' else '看板分析'
        assert f'aria-current="page">{expected}</a>' in switch
        sidebar = re.search(r'<nav[^>]*>(.*?)</nav>', page, re.S)[1]
        if route == '/master-data':
            assert sidebar.count('data-master=') == 6
            for fragment in ['materials', 'suppliers', 'warehouses', 'transfer-routes', 'material-controls', 'mapping']:
                assert f'href="#{fragment}"' in sidebar
            assert '采购到货核对' not in sidebar and '存销看板' not in sidebar
            assert 'class="workspace-tabs"' not in page
            assert 'id="masterImport"' in page and 'id="mappingImport"' in page
        else:
            assert sidebar.count('<a ') == 4
            assert '存销看板' in sidebar and '采购到货核对' in sidebar
            assert '物料管控表' not in sidebar and '仓库映射' not in sidebar
