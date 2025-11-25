from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser
from datetime import datetime
from contextlib import asynccontextmanager
import re
import logging
import os
import asyncio

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
    await browser.close()
    await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

class CPFRequest(BaseModel):
    cpf: str

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
        page.locator("button:has-text('Aceitar'), .cc-btn.cc-dismiss").click(timeout=2000).catch(lambda _: None)
        
        # Verificar se não há resultados
        if await page.get_by_text("Nenhum registro encontrado").is_visible():
            logger.info("No records found for this CPF")
            return {"result": "pesquisar", "message": "CPF not found or no data"}

        # Verificar se tem aposentado
        row_with_aposentado = page.locator("tr:has-text('Aposentado')").first
        
        if not await row_with_aposentado.is_visible():
            return {"result": "pesquisar", "message": "Status is not 'Aposentado'"}
        
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

# Health check endpoint
@app.get("/health")
async def health():
    return {"status": "ok", "browser_ready": browser is not None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)