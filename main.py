from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser
from datetime import datetime
from contextlib import asynccontextmanager
import re
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global variables for Playwright
playwright_instance = None
browser: Browser = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser
    logger.info("Starting Playwright...")
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True, 
        args=["--no-sandbox", "--disable-setuid-sandbox"]
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

async def block_resources(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

@app.post("/consultar")
async def consultar_cpf(request: CPFRequest):
    cpf = request.cpf
    # Basic CPF sanitization
    cpf_clean = re.sub(r"\D", "", cpf)
    
    if len(cpf_clean) != 11:
         raise HTTPException(status_code=400, detail="Invalid CPF format")

    if not browser:
        raise HTTPException(status_code=500, detail="Browser not initialized")

    # Create a new context for each request to ensure isolation, but reuse the browser
    context = await browser.new_context()
    page = await context.new_page()
    
    # Enable resource blocking
    await page.route("**/*", block_resources)

    try:
        logger.info(f"Starting search for CPF: {cpf}")
        await page.goto("https://portaldatransparencia.gov.br/servidores/consulta?ordenarPor=nome&direcao=asc", timeout=60000)
        
        # Accept cookies if the banner appears (fast check)
        try:
            cookie_btn = page.locator("button", has_text="Aceitar").or_(page.locator(".cc-btn.cc-dismiss"))
            if await cookie_btn.is_visible(timeout=2000):
                await cookie_btn.click()
        except:
            pass

        # Click on CPF filter in the sidebar
        await page.get_by_role("button", name="CPF").click()
        
        # Wait for the sidebar input to appear and type CPF
        await page.locator("input#cpf").fill(cpf_clean)
        
        if await page.get_by_role("button", name="Adicionar").is_visible():
                await page.get_by_role("button", name="Adicionar").click()

        await page.get_by_role("button", name="Consultar").click()
        
        # Wait for results
        try:
            await page.wait_for_selector("#tabela-resultado", timeout=30000)
        except:
             return {"result": "pesquisar", "message": "Timeout waiting for results"}
        
        # Check if there are results. If "Nenhum registro encontrado", return "pesquisar".
        if await page.get_by_text("Nenhum registro encontrado").is_visible():
            return {"result": "pesquisar", "message": "CPF not found or no data"}

        row_with_aposentado = page.locator("tr", has_text="Aposentado").first
        
        if await row_with_aposentado.is_visible():
            logger.info("Status 'Aposentado' found.")
            
            # Click on the "eye" icon in the "Detalhar" column
            try:
                await row_with_aposentado.locator("a .fa-eye").first.click(timeout=2000)
            except:
                try:
                    await row_with_aposentado.locator("a[title*='Detalhar']").click(timeout=2000)
                except:
                        await row_with_aposentado.locator("td").last.locator("a").click()
            
            # Wait for details page
            await page.wait_for_load_state("domcontentloaded") # Faster than networkidle
            
            # Click "Histórico dos vínculos com o poder executivo federal"
            try:
                await page.get_by_text("Histórico dos vínculos com o poder executivo federal").click(timeout=5000)
            except:
                 # Sometimes it might be already open or different layout
                 pass
            
            # Extract retirement date
            date_locator = page.get_by_text("Data da aposentadoria", exact=False).or_(page.get_by_text("Data de início do vínculo", exact=False))
            
            if await date_locator.count() > 0:
                full_text = await page.content()
                match = re.search(r"(?:Data da aposentadoria|Data de início do vínculo).*?(\d{2}/\d{2}/\d{4})", full_text, re.IGNORECASE | re.DOTALL)
                
                if match:
                    date_str = match.group(1)
                    date_obj = parse_date(date_str)
                    
                    if date_obj:
                        dec_2003 = datetime(2003, 12, 1)
                        if date_obj > dec_2003:
                            return {"result": "descarte", "date": date_str}
                        else:
                            return {"result": "pesquisar", "date": date_str}
                    else:
                            return {"status": "error", "message": "Could not parse date"}
                else:
                        return {"status": "error", "message": "Date not found in text"}
            else:
                return {"status": "error", "message": "Date field not found"}

        else:
            return {"result": "pesquisar", "message": "Status is not 'Aposentado'"}

    except Exception as e:
        logger.error(f"Error processing CPF {cpf}: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        await context.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
