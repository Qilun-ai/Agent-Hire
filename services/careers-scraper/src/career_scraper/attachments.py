"""Extract every page of a published job attachment; fail explicitly on missing text."""
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from pypdf import PdfReader


def extract_attachment(content, filename):
    suffix = Path(filename).suffix.lower()
    converted = False
    if content.startswith(b'%PDF-'):
        pdf = content
    elif suffix in {'.doc', '.docx'}:
        office = os.environ.get('CAREERS_SOFFICE') or shutil.which('soffice')
        if not office:
            raise ValueError('LibreOffice is required to read this published Word attachment')
        with tempfile.TemporaryDirectory(prefix='careers-job-document-') as directory:
            root = Path(directory)
            source = root / ('input' + suffix)
            source.write_bytes(content)
            profile = root / 'office-profile'
            profile.mkdir()
            subprocess.run([office, '--headless', f'-env:UserInstallation={profile.as_uri()}',
                            '--convert-to', 'pdf', '--outdir', str(root), str(source)],
                           check=True, timeout=90, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            target = root / 'input.pdf'
            if not target.exists():
                raise ValueError('Published Word attachment could not be converted for full-page reading')
            pdf = target.read_bytes()
        converted = True
    else:
        raise ValueError(f'Unsupported or invalid published attachment: {suffix}')
    reader = PdfReader(BytesIO(pdf))
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('Published attachment is encrypted; its full description is unavailable')
    pages = [page.extract_text(extraction_mode='layout') or '' for page in reader.pages]
    text = '\n\n'.join(pages).strip()
    if not pages or len(text) < 100:
        raise ValueError('Attachment has no complete extractable text; rendered/OCR review is required')
    for page, page_text in zip(reader.pages, pages):
        for resource in (page.get('/Resources') or {}).get('/XObject', {}).values():
            image = resource.get_object()
            if (image.get('/Subtype') == '/Image' and image.get('/Width', 0) > 800
                    and image.get('/Height', 0) > 800 and len(page_text.strip()) < 500):
                raise ValueError('Attachment contains a possible scanned job-description page; OCR review is required')
    return {'text': text, 'pages': len(pages), 'page_text_lengths': [len(t) for t in pages],
            'method': 'libreoffice.pdf+pypdf.all_pages' if converted else 'pypdf.all_pages'}
