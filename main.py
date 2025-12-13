from fastapi import FastAPI, HTTPException, File, UploadFile, BackgroundTasks, Form
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser
from datetime import datetime
from contextlib import asynccontextmanager
import re
import logging
import os
import asyncio
import pdfplumber
import io
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROXY_SERVER = os.getenv("PROXY_SERVER")
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

playwright_instance = None
browser: Browser = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser
    logger.info("Starting Playwright...")
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",  # Reduz uso de memória
            "--disable-gpu",  # Não precisa de GPU em headless
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--mute-audio",
            "--no-first-run",
            "--safebrowsing-disable-auto-update",
        ]
    )
    yield
    logger.info("Shutting down Playwright...")
    if browser:
        await browser.close()
    if playwright_instance:
        await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

class CPFRequest(BaseModel):
    cpf: str

class PDFUrlRequest(BaseModel):
    url: str

class AsyncPDFUrlRequest(BaseModel):
    url: str
    webhook_url: str
    request_id: str

def extract_cpf_and_name_from_text(text, extracted_data):
    lines = text.split('\n')
    for line in lines:
        # Skip header lines
        if 'MATRIC' in line.upper() or ('NOME' in line.upper() and 'CIC' in line.upper()):
            continue

        # Regex flexível para CPF
        search_match = re.search(r'(\d{3}\.?\d{3}\.?\d{3}-?\s?\d{2})', line)
        
        if search_match:
            raw_cpf = search_match.group(1)
            clean_cpf = re.sub(r'\D', '', raw_cpf)
            
            if len(clean_cpf) != 11:
                continue
                
            match_start_index = search_match.start()
            potential_text = line[:match_start_index]
            potential_text = re.sub(r'^\s*\d+\s+', '', potential_text)
            name = re.sub(r'[^\w\sáéíóúÁÉÍÓÚâêîôûÂÊÎÔÛãõÃÕçÇ]', '', potential_text).strip()
            
            if len(name) > 3 and not name.isnumeric():
                extracted_data.append({"name": name, "cpf": clean_cpf})
            else:
                extracted_data.append({"name": "Nome não identificado", "cpf": clean_cpf})

def extract_data_from_bytes(pdf_bytes):
    file_obj = io.BytesIO(pdf_bytes)
    extracted_data = []
    use_ocr = False
    
    # First, try pdfplumber (faster, for text-based PDFs)
    with pdfplumber.open(file_obj) as pdf:
        total_pages = len(pdf.pages)
        logger.info(f"Processing PDF with {total_pages} pages...")
        
        # Check if first few pages have text
        pages_with_text = 0
        for i in range(min(5, total_pages)):
            text = pdf.pages[i].extract_text()
            if text and len(text.strip()) > 50:
                pages_with_text += 1
        
        if pages_with_text == 0:
            logger.info("No text found in first pages, switching to OCR mode...")
            use_ocr = True
        else:
            logger.info(f"Found text in {pages_with_text}/5 first pages, using pdfplumber...")
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if not text:
                    continue
                extract_cpf_and_name_from_text(text, extracted_data)
    
    # If pdfplumber didn't find text, use OCR
    if use_ocr:
        logger.info(f"Starting OCR processing for all {total_pages} pages...")
        try:
            # Convert PDF to images - process ALL pages
            images = convert_from_bytes(pdf_bytes, dpi=150)  # Lower DPI for faster processing
            logger.info(f"Converted {len(images)} pages to images for OCR")
            
            for i, image in enumerate(images):
                # Apply OCR
                text = pytesseract.image_to_string(image, lang='por')
                # Progress log every 10 pages
                if (i + 1) % 10 == 0:
                    logger.info(f"OCR Progress: {i+1}/{len(images)} pages processed, {len(extracted_data)} records extracted so far")
                if text:
                    extract_cpf_and_name_from_text(text, extracted_data)
        except Exception as ocr_error:
            logger.error(f"OCR processing failed: {ocr_error}")
            # Don't raise here, return what we have or empty
            pass

    logger.info(f"Extracted {len(extracted_data)} records")
    return extracted_data

async def process_pdf_bytes_background(pdf_bytes: bytes, webhook_url: str, request_id: str):
    logger.info(f"Starting background processing for request {request_id}")
    try:
        extracted_data = extract_data_from_bytes(pdf_bytes)
        
        # Send webhook
        webhook_payload = {
            "requestId": request_id,
            "data": extracted_data,
            "status": "success"
        }
        
        logger.info(f"Sending webhook to {webhook_url} with {len(extracted_data)} records")
        async with httpx.AsyncClient() as client:
            response = await client.post(webhook_url, json=webhook_payload, timeout=30.0)
            logger.info(f"Webhook response: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Error in background extraction: {e}")
        try:
             async with httpx.AsyncClient() as client:
                await client.post(webhook_url, json={
                    "requestId": request_id, 
                    "status": "error",
                    "error": str(e)
                })
        except:
            pass

@app.post("/extract-pdf-async")
async def extract_pdf_async(
    file: UploadFile = File(...), 
    webhook_url: str = Form(...), 
    request_id: str = Form(...),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    logger.info(f"Received async PDF extract request {request_id}")
    file_bytes = await file.read()
    background_tasks.add_task(process_pdf_bytes_background, file_bytes, webhook_url, request_id)
    return {"status": "processing_background"}

@app.post("/extract-pdf-url-async")
async def extract_pdf_url_async(request: AsyncPDFUrlRequest, background_tasks: BackgroundTasks):
    logger.info(f"Received async PDF URL extract request {request.request_id}")
    
    async def process_url_wrapper(url, webhook, req_id):
        try:
             async with httpx.AsyncClient() as client:
                response = await client.get(url, follow_redirects=True, timeout=120.0)
                if response.status_code != 200:
                    raise Exception(f"Failed to download PDF: {response.status_code}")
                pdf_bytes = response.content
                await process_pdf_bytes_background(pdf_bytes, webhook, req_id)
        except Exception as e:
            logger.error(f"Download error: {e}")
             # Report error via webhook if possible
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(webhook, json={"requestId": req_id, "status": "error", "error": str(e)})
            except: pass

    background_tasks.add_task(process_url_wrapper, request.url, request.webhook_url, request.request_id)
    return {"status": "processing_background"}

def parse_date(date_str: str):
    try:
        return datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return None

# Lista de domínios para bloquear (analytics, ads, etc.)
BLOCKED_DOMAINS = [
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "doubleclick.net",
    "analytics",
    "hotjar.com",
    "clarity.ms",
]

async def block_resources(route):
    url = route.request.url.lower()
    resource_type = route.request.resource_type
    
    # Bloquear tipos de recursos desnecessários
    if resource_type in ["image", "media", "font", "websocket", "manifest"]:
        await route.abort()
        return
    
    # Bloquear domínios de analytics/tracking
    if any(domain in url for domain in BLOCKED_DOMAINS):
        await route.abort()
        return
    
    await route.continue_()

@app.post("/consultar")
async def consultar_cpf(request: CPFRequest):
    cpf = request.cpf
    cpf_clean = re.sub(r"\D", "", cpf)
    
    if len(cpf_clean) != 11:
        raise HTTPException(status_code=400, detail="Invalid CPF format")

    if not browser:
        raise HTTPException(status_code=500, detail="Browser not initialized")

    context_options = {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "java_script_enabled": True,
        "ignore_https_errors": True,
    }
    
    if PROXY_SERVER:
        proxy_config = {"server": PROXY_SERVER}
        if PROXY_USERNAME and PROXY_PASSWORD:
            proxy_config["username"] = PROXY_USERNAME
            proxy_config["password"] = PROXY_PASSWORD
        context_options["proxy"] = proxy_config
        logger.info(f"Using proxy: {PROXY_SERVER}")
    
    context = await browser.new_context(**context_options)
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    
    page = await context.new_page()
    await page.route("**/*", block_resources)
    
    # Timeouts mais agressivos
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(45000)

    try:
        logger.info(f"Starting search for CPF: {cpf_clean}")
        # OTIMIZAÇÃO 1: Navegação direta para a URL de busca com os parâmetros
        search_url = (
            f"https://portaldatransparencia.gov.br/servidores/consulta?"
            f"paginacaoSimples=true&tamanhoPagina=&offset=&direcaoOrdenacao=asc"
            f"&cpf={cpf_clean}&colunasSelecionadas=detalhar%2Ctipo%2Ccpf%2Cnome%2C"
            f"orgaoServidorLotacao%2Cmatricula%2Csituacao%2Cfuncao%2Ccargo%2Cquantidade"
        )
        
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        
        # Tentar fechar cookie banner se aparecer (não bloqueante)
        try:
            await page.locator("button:has-text('Aceitar'), .cc-btn.cc-dismiss").click(timeout=2000)
        except:
            pass
        
        # Verificar se não há resultados
        if await page.get_by_text("Nenhum registro encontrado").is_visible():
            logger.info("No records found for this CPF")
            return {"result": "descarte", "message": "CPF não encontrado"}

        # Verificar se tem aposentado
        row_with_aposentado = page.locator("tr:has-text('Aposentado')").first
        
        if not await row_with_aposentado.is_visible():
            # Se encontrou o CPF mas NÃO tem vínculo como Aposentado (ex: Ativo), descarta.
            return {"result": "descarte", "message": "Status is not 'Aposentado'"}
        
        logger.info("Status 'Aposentado' found.")
        
        # OTIMIZAÇÃO 6: Clicar no link de detalhes de forma mais direta
        detail_link = row_with_aposentado.locator("a").last
        await detail_link.click()

        # Esperar navegação parcial
        await page.wait_for_load_state("domcontentloaded")
        
        # Tentar expandir histórico (não crítico se falhar)
        try:
            await page.get_by_text("Histórico dos vínculos com o poder executivo federal").click(timeout=5000)
            await asyncio.sleep(0.5)  # Pequena espera para o conteúdo expandir
        except:
            pass
        
        # OTIMIZAÇÃO 7: Buscar data de forma mais eficiente
        # Primeiro tenta locator específico, depois fallback para regex
        date_patterns = [
            r"Data da aposentadoria[:\s]*(\d{2}/\d{2}/\d{4})",
            r"Data de início do vínculo[:\s]*(\d{2}/\d{2}/\d{4})",
        ]
        
        # Pegar apenas a parte relevante do HTML (mais rápido que page.content() completo)
        try:
            main_content = await page.locator("main, .conteudo-principal, #conteudo").first.inner_html(timeout=5000)
        except:
            main_content = await page.content()
        
        date_str = None
        for pattern in date_patterns:
            match = re.search(pattern, main_content, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                break
        
        if not date_str:
            return {"status": "error", "message": "Date not found in text"}
        
        date_obj = parse_date(date_str)
        if not date_obj:
            return {"status": "error", "message": "Could not parse date"}
        
        dec_2003 = datetime(2003, 12, 1)
        if date_obj > dec_2003:
            return {"result": "descarte", "date": date_str}
        else:
            return {"result": "pesquisar", "date": date_str}

    except Exception as e:
        logger.error(f"Error processing CPF {cpf}: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        await context.close()

@app.get("/health")
async def health():
    return {"status": "ok", "browser_ready": browser is not None}

@app.post("/extract-pdf")
async def extract_pdf(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        extracted_data = extract_data_from_bytes(contents)
        return {"status": "success", "data": extracted_data}
    except Exception as e:
        logger.error(f"Error processing PDF: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/extract-pdf-url")
async def extract_pdf_url(request: PDFUrlRequest):
    try:
        logger.info(f"Downloading PDF from URL: {request.url}")
        async with httpx.AsyncClient() as client:
            response = await client.get(request.url, follow_redirects=True, timeout=120.0)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Failed to download PDF: {response.status_code}")
        
        pdf_bytes = response.content
        extracted_data = extract_data_from_bytes(pdf_bytes)
        
        return {"status": "success", "data": extracted_data}
    except Exception as e:
        logger.error(f"Error processing PDF from URL: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/version")
async def version():
    return {"version": "3.2", "features": ["ocr", "pdfplumber", "async-webhook", "receita-federal"]}

# ============================================
# RECEITA FEDERAL - CPF Status Check
# ============================================

class ReceitaFederalRequest(BaseModel):
    cpf: str
    data_nascimento: str  # Format: DD/MM/YYYY

@app.post("/consultar-receita")
async def consultar_receita_federal(request: ReceitaFederalRequest):
    """
    Consulta situação cadastral do CPF na Receita Federal.
    Retorna se o titular está falecido ou regular.
    
    Args:
        cpf: CPF com 11 dígitos
        data_nascimento: Data de nascimento no formato DD/MM/YYYY
    
    Returns:
        situacao_cadastral: REGULAR, PENDENTE DE REGULARIZAÇÃO, SUSPENSA, CANCELADA POR ÓBITO, TITULAR FALECIDO, NULA
        nome: Nome do titular
        success: True se a consulta foi bem sucedida
    """
    cpf = request.cpf
    cpf_clean = re.sub(r"\D", "", cpf)
    
    if len(cpf_clean) != 11:
        raise HTTPException(status_code=400, detail="Invalid CPF format - must have 11 digits")
    
    # Validate date format
    data_nascimento = request.data_nascimento
    if not re.match(r"\d{2}/\d{2}/\d{4}", data_nascimento):
        raise HTTPException(status_code=400, detail="Invalid date format - use DD/MM/YYYY")
    
    if not browser:
        raise HTTPException(status_code=500, detail="Browser not initialized")
    
    context_options = {
        "viewport": {"width": 1280, "height": 720},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "java_script_enabled": True,
        "ignore_https_errors": True,
    }
    
    if PROXY_SERVER:
        proxy_config = {"server": PROXY_SERVER}
        if PROXY_USERNAME and PROXY_PASSWORD:
            proxy_config["username"] = PROXY_USERNAME
            proxy_config["password"] = PROXY_PASSWORD
        context_options["proxy"] = proxy_config
    
    context = await browser.new_context(**context_options)
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    
    page = await context.new_page()
    page.set_default_timeout(60000)
    page.set_default_navigation_timeout(60000)
    
    try:
        logger.info(f"Consulting Receita Federal for CPF: {cpf_clean}")
        
        # URL da consulta de situação cadastral
        rf_url = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"
        
        await page.goto(rf_url, wait_until="domcontentloaded", timeout=60000)
        logger.info("Loaded Receita Federal page")
        
        # Wait for form to be ready
        await page.wait_for_selector('input[name="txtCPF"]', timeout=30000)
        
        # Fill CPF field
        await page.fill('input[name="txtCPF"]', cpf_clean)
        logger.info(f"Filled CPF: {cpf_clean}")
        
        # Fill birth date field
        await page.fill('input[name="txtDataNascimento"]', data_nascimento)
        logger.info(f"Filled birth date: {data_nascimento}")
        
        # Click captcha checkbox (hCaptcha or similar)
        # The captcha is usually an iframe, we need to wait for it
        try:
            # Try to find and click the captcha checkbox
            captcha_frame = page.frame_locator('iframe[title*="captcha"], iframe[src*="hcaptcha"], iframe[src*="recaptcha"]')
            await captcha_frame.locator('div[role="checkbox"], .check').click(timeout=10000)
            logger.info("Clicked captcha checkbox")
            
            # Wait a bit for captcha to resolve
            await asyncio.sleep(2)
        except Exception as captcha_error:
            logger.warning(f"Captcha interaction issue: {captcha_error}")
            # Try clicking submit anyway - some cases don't have captcha
        
        # Click the submit/search button
        submit_button = page.locator('input[type="submit"], button[type="submit"], input[value*="Consultar"]')
        await submit_button.click()
        logger.info("Clicked submit button")
        
        # Wait for result page
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1)  # Small delay for content to render
        
        # Get page content to extract result
        page_content = await page.content()
        
        # Extract situação cadastral
        situacao_pattern = r"Situação Cadastral[:\s]*<[^>]*>([^<]+)"
        situacao_match = re.search(situacao_pattern, page_content, re.IGNORECASE)
        
        # Alternative pattern for plain text
        if not situacao_match:
            situacao_pattern_alt = r"Situação Cadastral[:\s]*(\w+(?:\s+\w+)*)"
            situacao_match = re.search(situacao_pattern_alt, page_content, re.IGNORECASE)
        
        # Try to find common status indicators
        situacao_cadastral = None
        if "TITULAR FALECIDO" in page_content.upper():
            situacao_cadastral = "TITULAR FALECIDO"
        elif "CANCELADA POR ÓBITO" in page_content.upper():
            situacao_cadastral = "CANCELADA POR ÓBITO"
        elif "REGULAR" in page_content.upper() and "SITUAÇÃO CADASTRAL" in page_content.upper():
            situacao_cadastral = "REGULAR"
        elif "PENDENTE" in page_content.upper():
            situacao_cadastral = "PENDENTE DE REGULARIZAÇÃO"
        elif "SUSPENSA" in page_content.upper():
            situacao_cadastral = "SUSPENSA"
        elif "NULA" in page_content.upper():
            situacao_cadastral = "NULA"
        elif situacao_match:
            situacao_cadastral = situacao_match.group(1).strip()
        
        # Extract name
        nome_pattern = r"Nome[:\s]*<[^>]*>([^<]+)"
        nome_match = re.search(nome_pattern, page_content, re.IGNORECASE)
        nome = nome_match.group(1).strip() if nome_match else None
        
        # Check if consultation was successful
        if situacao_cadastral:
            is_deceased = situacao_cadastral.upper() in ["TITULAR FALECIDO", "CANCELADA POR ÓBITO"]
            
            logger.info(f"Receita Federal result for {cpf_clean}: {situacao_cadastral}")
            return {
                "cpf": cpf_clean,
                "situacao_cadastral": situacao_cadastral,
                "nome": nome,
                "is_deceased": is_deceased,
                "data_consulta": datetime.now().isoformat(),
                "success": True
            }
        else:
            # Check for error messages
            if "CPF não encontrado" in page_content or "dados informados não conferem" in page_content.lower():
                return {
                    "cpf": cpf_clean,
                    "situacao_cadastral": None,
                    "nome": None,
                    "is_deceased": False,
                    "data_consulta": datetime.now().isoformat(),
                    "success": False,
                    "error": "CPF não encontrado ou dados não conferem"
                }
            
            return {
                "cpf": cpf_clean,
                "situacao_cadastral": None,
                "nome": None,
                "is_deceased": False,
                "data_consulta": datetime.now().isoformat(),
                "success": False,
                "error": "Could not extract status from page"
            }
    
    except Exception as e:
        logger.error(f"Error consulting Receita Federal for CPF {cpf_clean}: {e}")
        return {
            "cpf": cpf_clean,
            "situacao_cadastral": None,
            "nome": None,
            "is_deceased": False,
            "data_consulta": datetime.now().isoformat(),
            "success": False,
            "error": str(e)
        }
    finally:
        await context.close()


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting RPA Service v3.2 - With Receita Federal Support")
    uvicorn.run(app, host="0.0.0.0", port=8000)