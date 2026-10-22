from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from contextlib import closing
import sqlite3

from PIL import Image, PngImagePlugin
import pytest

import engineering_hub.label_sets as module
from engineering_hub.label_sets import LabelSetRepository
from engineering_hub.repository import HubError, HubRepository


@pytest.fixture
def labels(tmp_path):
    return LabelSetRepository(HubRepository(tmp_path / 'hub.sqlite3'))


def create(labels, **changes):
    args = dict(set_id='crowns', project='treedar', title='Crown review', instructions='Choose a species.',
                choices=[{'key': 'oak', 'label': 'Oak'}, {'key': 'pine', 'label': 'Pine'}],
                items=[{'id': 'crown-1', 'meta': {'confidence': 0.75, 'source': 'test'}}], actor='claude')
    args.update(changes)
    return labels.create(**args)


def picture(kind='PNG', color='green', **options):
    buffer = BytesIO()
    with Image.new('RGB', (8, 8), color) as image:
        image.save(buffer, format=kind, **options)
    return buffer.getvalue()


def test_decoder_concurrency_is_bounded_and_recovers():
    assert module._DECODER.acquire(blocking=False)
    try:
        with pytest.raises(HubError) as caught:
            module._image(picture(), 'image/png')
        assert caught.value.code == 'image_decoder_busy'
        assert caught.value.status == 429
    finally:
        module._DECODER.release()
    assert module._image(picture(), 'image/png')
    with pytest.raises(HubError):
        module._image(b'not an image', 'image/png')
    assert module._image(picture(), 'image/png')


def test_persistence_contract_idempotence_and_audit(labels):
    first = create(labels)
    assert first['items'][0]['label'] is None
    assert first['items'][0]['revision'] == 0
    assert create(labels) == first
    assert len(labels.repo.list_audit()) == len(labels.repo.list_outbox()) == 1
    reopened = LabelSetRepository(HubRepository(labels.repo.path))
    assert reopened.get('crowns') == first
    assert reopened.results('crowns') == first
    assert reopened.list('treedar')['items'] == [dict(
        id='crowns', project='treedar', title='Crown review', item_count=1,
        labeled_count=0, updated_at=first['updated_at'])]
    with pytest.raises(HubError, match='label_set_conflict'):
        create(labels, title='Changed')
    assert len(labels.repo.list_audit()) == 1


@pytest.mark.parametrize('changes', [
    {'set_id': '../escape'}, {'set_id': 'x..y'}, {'project': 'other'}, {'title': ''},
    {'title': 'a' * 201}, {'instructions': 'a' * 4097}, {'choices': []},
    {'choices': [{'key': 'a', 'label': 'A'}] * 2},
    {'choices': [{'key': 'a', 'label': 'A', 'html': '<x>'}, {'key': 'b', 'label': 'B'}]},
    {'items': []}, {'items': [{'id': 'x'}] * 2}, {'items': [{'id': 'x', 'label': 'oak'}]},
    {'items': [{'id': 'x', 'meta': {'constructor': 'a'}}]},
    {'items': [{'id': 'x', 'meta': {'__proto__': 'a'}}]},
    {'items': [{'id': 'x', 'meta': {'valid': True}}]},
    {'items': [{'id': 'x', 'meta': {'valid': float('nan')}}]},
    {'items': [{'id': 'x', 'meta': {'valid': float('inf')}}]},
    {'items': [{'id': 'x', 'meta': {'valid': 10 ** 1000}}]},
    {'items': [{'id': 'x', 'meta': {'valid': []}}]},
    {'items': [{'id': 'x', 'meta': {'valid': 'a' * 121}}]},
    {'items': [{'id': 'x', 'meta': {str(i): i for i in range(9)}}]},
    {'actor': 'human:1'}, {'actor': 'unknown'},
])
def test_invalid_create_is_atomic(labels, changes):
    with pytest.raises(HubError):
        create(labels, **changes)
    assert labels.list('treedar') == {'items': []}
    assert labels.repo.list_audit() == labels.repo.list_outbox() == []


def test_human_label_cas_clear_and_agent_denial(labels):
    create(labels)
    labels.upload_image('crowns', 'crown-1', content=picture(), content_type='image/png', actor='codex')
    saved = labels.label('crowns', 'crown-1', choice='oak', expected_revision=0, actor='human:1')
    assert saved['revision'] == 1
    assert saved['label']['choice'] == 'oak'
    assert saved['label']['by'] == 'human:1'
    assert labels.list('treedar')['items'][0]['labeled_count'] == 1
    for actor in ['claude', 'human:bad', 'human:0']:
        with pytest.raises(HubError, match='forbidden'):
            labels.label('crowns', 'crown-1', choice='pine', expected_revision=1, actor=actor)
    for revision in [0, True, 1.0]:
        with pytest.raises(HubError, match='label_conflict'):
            labels.label('crowns', 'crown-1', choice='pine', expected_revision=revision, actor='human:1')
    with pytest.raises(HubError, match='invalid_choice'):
        labels.label('crowns', 'crown-1', choice='other', expected_revision=1, actor='human:1')
    cleared = labels.label('crowns', 'crown-1', choice=None, expected_revision=1,
                           actor='human:cf:' + 'a' * 64)
    assert cleared['revision'] == 2 and cleared['label'] is None
    assert len(labels.repo.list_audit()) == 4


def test_racing_labels_one_winner(labels):
    create(labels)
    labels.upload_image('crowns', 'crown-1', content=picture(), content_type='image/png', actor='codex')
    def assign(choice):
        try:
            return labels.label('crowns', 'crown-1', choice=choice, expected_revision=0, actor='human:2')
        except HubError as exc:
            assert exc.code == 'label_conflict'
            return None
    with ThreadPoolExecutor(2) as pool:
        saved = list(pool.map(assign, ['oak', 'pine']))
    assert sum(result is not None for result in saved) == 1
    assert len(labels.repo.list_outbox()) == 3


@pytest.mark.parametrize('kind,content_type', [('PNG', 'image/png'), ('JPEG', 'image/jpeg')])
def test_image_decode_canonical_idempotence_and_export_separation(labels, kind, content_type):
    create(labels)
    payload = picture(kind) + b'untrusted trailing bytes'
    first = labels.upload_image('crowns', 'crown-1', content=payload, content_type=content_type, actor='codex')
    assert labels.upload_image('crowns', 'crown-1', content=payload, content_type=content_type, actor='hermes') == first
    clean, media = labels.image('crowns', 'crown-1')
    assert media == content_type and b'untrusted trailing bytes' not in clean
    assert first['bytes'] == len(clean)
    assert len(labels.repo.list_audit()) == 2
    assert 'data' not in labels.results('crowns')
    with pytest.raises(HubError, match='label_image_conflict'):
        labels.upload_image('crowns', 'crown-1', content=picture(kind, 'blue'), content_type=content_type, actor='codex')


def test_png_metadata_removed(labels):
    create(labels)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('Comment', '<script>not retained</script>')
    labels.upload_image('crowns', 'crown-1', content=picture(pnginfo=metadata), content_type='image/png', actor='codex')
    with Image.open(BytesIO(labels.image('crowns', 'crown-1')[0])) as clean:
        assert clean.info == {}


@pytest.mark.parametrize('payload,media', [
    (b'not an image', 'image/png'), (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xffbroken', 'image/jpeg'), (picture(), 'image/jpeg'),
    (picture('JPEG'), 'image/png'), (picture(), 'text/html'),
    (picture()[:40], 'image/png'), (b'x' * (256 * 1024 + 1), 'image/png'),
], ids=['text', 'png-magic-only', 'jpeg-magic-only', 'png-mime-mismatch',
        'jpeg-mime-mismatch', 'html-mime', 'truncated', 'oversized'])
def test_invalid_image_no_mutation(labels, payload, media):
    create(labels)
    with pytest.raises(HubError, match='invalid_label_image'):
        labels.upload_image('crowns', 'crown-1', content=payload, content_type=media, actor='codex')
    assert len(labels.repo.list_audit()) == 1
    with pytest.raises(HubError, match='label_image_not_found'):
        labels.image('crowns', 'crown-1')


def test_image_pixel_animation_and_output_caps(labels, monkeypatch):
    create(labels)
    monkeypatch.setattr(module, 'MAX_PIXELS', 63)
    with pytest.raises(HubError, match='invalid_label_image'):
        labels.upload_image('crowns', 'crown-1', content=picture(), content_type='image/png', actor='codex')
    monkeypatch.setattr(module, 'MAX_PIXELS', 16_000_000)
    output = BytesIO()
    with Image.new('RGB', (2, 2), 'red') as one, Image.new('RGB', (2, 2), 'blue') as two:
        one.save(output, format='PNG', save_all=True, append_images=[two])
    with pytest.raises(HubError, match='invalid_label_image'):
        labels.upload_image('crowns', 'crown-1', content=output.getvalue(), content_type='image/png', actor='codex')
    # JPEG canonical bytes can exceed the original tiny PNG; cap the encoded output independently.
    payload = picture()
    monkeypatch.setattr(module, 'MAX_IMAGE_BYTES', len(payload))
    with pytest.raises(HubError, match='invalid_label_image'):
        labels.upload_image('crowns', 'crown-1', content=payload, content_type='image/png', actor='codex')


def test_transactional_quotas(labels, monkeypatch):
    create(labels, items=[{'id': 'a'}, {'id': 'b'}])
    first = labels.upload_image('crowns', 'a', content=picture(), content_type='image/png', actor='codex')
    monkeypatch.setattr(module, 'MAX_SET_BYTES', first['bytes'])
    with pytest.raises(HubError, match='label_image_quota'):
        labels.upload_image('crowns', 'b', content=picture(), content_type='image/png', actor='codex')
    monkeypatch.setattr(module, 'MAX_SET_BYTES', 25 * 1024 * 1024)
    monkeypatch.setattr(module, 'MAX_TOTAL_BYTES', first['bytes'])
    create(labels, set_id='second')
    with pytest.raises(HubError, match='label_image_quota'):
        labels.upload_image('second', 'crown-1', content=picture(), content_type='image/png', actor='codex')
    monkeypatch.setattr(module, 'MAX_SETS_PER_PROJECT', 2)
    with pytest.raises(HubError, match='label_set_limit'):
        create(labels, set_id='third')
    assert len(labels.repo.list_audit()) == 3


def test_outbox_failure_rolls_back_all_writes(labels, monkeypatch):
    create(labels, items=[{'id': 'crown-1'}, {'id': 'crown-2'}])
    labels.upload_image('crowns', 'crown-1', content=picture(), content_type='image/png', actor='codex')
    def fail(*args):
        raise sqlite3.OperationalError('simulated')
    monkeypatch.setattr(labels.repo, '_emit', fail)
    with pytest.raises(sqlite3.OperationalError):
        labels.label('crowns', 'crown-1', choice='oak', expected_revision=0, actor='human:1')
    with pytest.raises(sqlite3.OperationalError):
        labels.upload_image('crowns', 'crown-2', content=picture(), content_type='image/png', actor='codex')
    with pytest.raises(sqlite3.OperationalError):
        create(labels, set_id='second')
    assert labels.get('crowns')['items'][0]['revision'] == 0
    assert len(labels.list('treedar')['items']) == 1
    assert len(labels.repo.list_audit()) == 2
    with pytest.raises(HubError, match='label_image_not_found'):
        labels.image('crowns', 'crown-2')


def test_no_arbitrary_path_or_bypass_enrolled_ledger(labels):
    with pytest.raises(TypeError):
        LabelSetRepository(labels.repo.path)
    create(labels)
    # A repo object made before enrollment must not become an owner bypass.
    with closing(sqlite3.connect(labels.repo.path)) as db:
        db.execute('CREATE TABLE hub_pilot_owner (id TEXT)')
        db.commit()
    for operation in [lambda: LabelSetRepository(labels.repo),
                      lambda: create(labels, set_id='second'),
                      lambda: labels.label('crowns', 'crown-1', choice='oak', expected_revision=0, actor='human:1'),
                      lambda: labels.upload_image('crowns', 'crown-1', content=picture(), content_type='image/png', actor='codex')]:
        with pytest.raises(HubError, match='pilot_ownership_required'):
            operation()
    assert labels.get('crowns')['items'][0]['revision'] == 0


def test_outbox_events_do_not_contain_label_or_image_payloads(labels):
    create(labels)
    labels.upload_image('crowns', 'crown-1', content=picture(), content_type='image/png', actor='codex')
    labels.label('crowns', 'crown-1', choice='oak', expected_revision=0, actor='human:1')
    assert all(set(event) == {'sequence', 'action', 'entity_id', 'revision', 'created_at'}
               for event in labels.repo.list_outbox())


def test_image_budget_shared_with_entity_attachments(labels, monkeypatch):
    create(labels)
    with labels.repo._transaction() as db:
        db.execute('CREATE TABLE hub_entity_images(data BLOB NOT NULL)')
        db.execute('INSERT INTO hub_entity_images VALUES(?)', (b'existing attachment',))
    monkeypatch.setattr(module, 'MAX_TOTAL_BYTES', len(b'existing attachment'))
    with pytest.raises(HubError, match='label_image_quota'):
        labels.upload_image('crowns', 'crown-1', content=picture(), content_type='image/png', actor='codex')


def test_items_global_quota_and_image_requirement(labels, monkeypatch):
    create(labels)
    monkeypatch.setattr(module, 'MAX_ITEMS_GLOBAL', 1)
    with pytest.raises(HubError, match='label_set_limit'):
        create(labels, set_id='second')
    with pytest.raises(HubError, match='label_image_required'):
        labels.label('crowns', 'crown-1', choice='oak', expected_revision=0, actor='human:1')
    assert len(labels.repo.list_audit()) == 1
