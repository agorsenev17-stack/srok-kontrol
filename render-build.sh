
#!/usr/bin/env bash
set -e

echo "Устанавливаем Tesseract OCR..."

apt-get update
apt-get install -y tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng

echo "Устанавливаем Python-зависимости..."

python -m pip install --upgrade pip
pip install -r requirements.txt

echo "Установка завершена!"
