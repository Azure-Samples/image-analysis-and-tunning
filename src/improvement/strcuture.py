#!/usr/bin/env python3
"""
edit_image.py - Edit an image using the OpenAI gpt-image-1 model via the Images Edits endpoint.

Usage examples:
  # Set your API key in PowerShell:
  $env:OPENAI_API_KEY = "sk-..."

  # Edit the default image with the default prompt
  python edit_image.py

  # Provide custom input, prompt, and output
  python edit_image.py -i images/space-cat.jpg -p "Add a sunset background to the image, photorealistic" -o images/space-cat-edited.png

Notes:
- Requires the 'requests' package: pip install requests
- The script uses the REST endpoint POST https://api.openai.com/v1/images/edits with model=gpt-image-1
- If you provide a mask image (PNG), the transparent area is considered editable (see OpenAI docs for mask conventions).
"""

import os
import sys
import argparse
import base64
import mimetypes
import json

try:
    import requests
except ImportError:
    print("The 'requests' package is required. Install it with: pip install requests")
    sys.exit(1)

API_URL = "https://api.openai.com/v1/images/edits"


def guess_mime(path):
    mt = mimetypes.guess_type(path)[0]
    if mt:
        return mt
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.jpg', '.jpeg'):
        return 'image/jpeg'
    if ext == '.png':
        return 'image/png'
    return 'application/octet-stream'


def main():
    parser = argparse.ArgumentParser(description="Edit an image using the OpenAI gpt-image-1 model.")
    parser.add_argument("--input", "-i", default=".assets/0041128019.jpg", help="Path to input image")
    parser.add_argument("--mask", "-m", default=None, help="Path to mask PNG (optional). Transparent regions are editable.")
    parser.add_argument("--prompt", "-p", required=False, default="Give the person a luxurious head of hair like a lion mane, photorealistic", help="Edit prompt")
    parser.add_argument("--size", default="1024x1024", choices=['256x256','512x512','1024x1024'], help="Output size")
    parser.add_argument("--n", type=int, default=1, help="Number of outputs to create")
    parser.add_argument("--output", "-o", default="images_edited/estudiante.png", help="Output path (if --n>1, index will be added before extension)")
    args = parser.parse_args()

    # Load optional JSON config file for defaults. The script will look for
    # edit_image_config.json, edit_image.config.json, edit_image.json, or config.json
    config_paths = ['edit_image_config.json', 'edit_image.config.json', 'edit_image.json', 'config.json']
    cfg = {}
    for p in config_paths:
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as cf:
                    cfg = json.load(cf)
                # Found and parsed a config file; stop searching
                break
            except Exception as e:
                print(f"Failed to parse config file {p}: {e}")
                sys.exit(1)

    # Apply defaults from config and environment overrides so the script can run with no CLI args.
    args.input = os.getenv('EDIT_IMAGE_INPUT', cfg.get('input', args.input))
    args.mask = os.getenv('EDIT_IMAGE_MASK', cfg.get('mask', args.mask))
    args.prompt = os.getenv('EDIT_IMAGE_PROMPT', cfg.get('prompt', args.prompt))
    args.size = os.getenv('EDIT_IMAGE_SIZE', cfg.get('size', args.size))
    _env_n = os.getenv('EDIT_IMAGE_N')
    if _env_n is not None:
        try:
            args.n = int(_env_n)
        except Exception:
            print(f"Invalid EDIT_IMAGE_N value: {_env_n}. Must be an integer.")
            sys.exit(1)
    else:
        args.n = cfg.get('n', args.n)
    args.output = os.getenv('EDIT_IMAGE_OUTPUT', cfg.get('output', args.output))

    # Support either OpenAI or Azure OpenAI credentials (env first, then config)
    openai_key = os.getenv("OPENAI_API_KEY", cfg.get("openai_api_key"))
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", cfg.get("azure_openai_endpoint"))
    azure_key = os.getenv("AZURE_OPENAI_API_KEY", cfg.get("azure_openai_api_key"))
    if not (openai_key or (azure_endpoint and azure_key)):
        print("No valid credentials found. Set OPENAI_API_KEY for OpenAI, or set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY for Azure OpenAI.")
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"Input file not found: {args.input}")
        sys.exit(1)

    # Prepare multipart files
    files = []
    # The endpoint supports multiple input images using the field name 'image[]'
    files.append(('image[]', (os.path.basename(args.input), open(args.input, 'rb'), guess_mime(args.input))))

    if args.mask:
        if not os.path.exists(args.mask):
            print(f"Mask file not found: {args.mask}")
            sys.exit(1)
        # The mask field is typically named 'mask' and is a single PNG with transparent areas indicating where to edit
        files.append(('mask', (os.path.basename(args.mask), open(args.mask, 'rb'), guess_mime(args.mask))))

    # Build the request payload
    data = {
        'prompt': args.prompt,
        'size': args.size,
        'n': str(args.n),
    }

    # By default the public OpenAI API needs the 'model' key; Azure may use a deployment path instead
    if not azure_endpoint:
        data['model'] = 'gpt-image-1'

    # Build request URL and headers depending on provider
    if azure_endpoint:
        azure_endpoint = azure_endpoint.rstrip('/')
        azure_api_version = os.getenv('AZURE_OPENAI_API_VERSION', '2024-12-01-preview')
        azure_deployment = os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME')
        if azure_deployment:
            request_url = f"{azure_endpoint}/openai/deployments/{azure_deployment}/images/edits?api-version={azure_api_version}"
        else:
            request_url = f"{azure_endpoint}/openai/images/edits?api-version={azure_api_version}"
        headers = {"api-key": azure_key}
    else:
        request_url = API_URL
        headers = {"Authorization": f"Bearer {openai_key}"}

    print("Sending edit request to Azure OpenAI..." if azure_endpoint else "Sending edit request to OpenAI...")
    try:
        resp = requests.post(request_url, headers=headers, files=files, data=data)
    finally:
        # Close file handles
        for _, file_t in files:
            try:
                fileobj = file_t[1]
                if hasattr(fileobj, 'close'):
                    fileobj.close()
            except Exception:
                pass

    if resp.status_code != 200:
        print(f"Error: HTTP {resp.status_code}")
        try:
            err = resp.json()
            print(json.dumps(err, indent=2))
        except Exception:
            print(resp.text)
        sys.exit(1)

    try:
        j = resp.json()
    except ValueError:
        print("Invalid JSON response")
        print(resp.text)
        sys.exit(1)

    outputs = j.get('data', [])
    if not outputs:
        print("No images returned.")
        print(json.dumps(j, indent=2))
        sys.exit(1)

    out_base = args.output
    base, ext = os.path.splitext(out_base)
    for idx, item in enumerate(outputs):
        b64 = item.get('b64_json')
        url = item.get('url')
        if b64:
            img_bytes = base64.b64decode(b64)
            if args.n == 1:
                path = out_base
            else:
                path = f"{base}_{idx+1}{ext}"
            with open(path, 'wb') as f:
                f.write(img_bytes)
            print(f"Saved edited image to: {path}")
        elif url:
            r2 = requests.get(url)
            if r2.status_code == 200:
                if args.n == 1:
                    path = out_base
                else:
                    path = f"{base}_{idx+1}.png"
                with open(path, 'wb') as f:
                    f.write(r2.content)
                print(f"Saved edited image (from url) to: {path}")
            else:
                print("Failed to fetch image from URL:", url)
        else:
            print("Unknown response item:", item)


if __name__ == '__main__':
    main()
