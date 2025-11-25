# RPA Portal da Transparência

Este projeto é um Robô de Automação de Processos (RPA) desenvolvido em Python que consulta o status de aposentadoria de servidores públicos federais no Portal da Transparência do Governo Federal.

Ele foi projetado para ser executado como uma API REST (usando FastAPI) dentro de um container Docker, facilitando a integração com ferramentas de automação como n8n, Zapier, ou outros sistemas.

## 📋 Funcionalidades

-   **Consulta Automatizada**: Acessa o Portal da Transparência e busca por CPF.
-   **Análise de Vínculos**: Verifica o histórico de vínculos para identificar datas de aposentadoria.
-   **Regras de Negócio Inteligentes**:
    -   Se a data de aposentadoria for **após Dezembro de 2003** -> Retorna `descarte`.
    -   Se a data for anterior ou se não houver aposentadoria -> Retorna `pesquisar`.
    -   Se não encontrar registros -> Retorna `pesquisar`.
-   **Alta Performance**:
    -   Reutilização de instância do navegador (Chromium).
    -   Bloqueio de imagens, fontes e estilos para economia de dados e velocidade.
    -   Execução em modo `headless` (sem interface gráfica).

---

## 🚀 Como Executar Localmente

### Pré-requisitos

-   Python 3.11+
-   Docker (opcional, mas recomendado)

### Rodando com Python

1.  **Instale as dependências**:
    ```bash
    pip install -r requirements.txt
    ```
2.  **Instale os navegadores do Playwright**:
    ```bash
    playwright install chromium
    ```
3.  **Execute a aplicação**:
    ```bash
    uvicorn main:app --reload
    ```
    A API estará disponível em `http://localhost:8000`.

### Rodando com Docker

1.  **Construa a imagem**:
    ```bash
    docker build -t rpa-mariana .
    ```
2.  **Execute o container**:
    ```bash
    docker run -p 8000:8000 rpa-mariana
    ```

---

## 🔌 Documentação da API

### Endpoint: Consultar CPF

**POST** `/consultar`

Recebe um CPF e retorna o status da análise.

#### Corpo da Requisição (JSON)

```json
{
  "cpf": "123.456.789-00"
}
```

#### Respostas Possíveis

**1. Descarte (Aposentadoria recente)**
Indica que o servidor se aposentou após Dezembro de 2003.
```json
{
  "result": "descarte",
  "date": "15/05/2015"
}
```

**2. Pesquisar (Aposentadoria antiga ou não encontrado)**
Indica que o servidor se aposentou antes de 2003, ou não é aposentado, ou o CPF não foi encontrado.
```json
{
  "result": "pesquisar",
  "date": "01/02/1998" 
}
```
*Nota: O campo `date` pode não estar presente se nenhum registro for encontrado.*

---

## ☁️ Guia de Deploy no Easypanel

O Easypanel é uma interface moderna para gerenciar servidores Docker. Siga estes passos para colocar sua API no ar.

### Passo 1: Preparar o Projeto
Certifique-se de que seu projeto está no GitHub (ou outro git provider). O projeto já contém o `Dockerfile` configurado corretamente para o Easypanel.

### Passo 2: Criar o Serviço no Easypanel
1.  Acesse seu painel do Easypanel.
2.  Crie um novo **Project** (se ainda não tiver um).
3.  Dentro do projeto, clique em **+ Service**.
4.  Escolha **App** (Application).

### Passo 3: Configurar a Fonte (Source)
1.  Em **Source**, selecione **Git**.
2.  Cole a URL do seu repositório GitHub (ex: `https://github.com/arttkeller/rpa-mariana`).
3.  Se o repositório for privado, você precisará configurar um Token de Acesso (Deploy Token) ou conectar sua conta do GitHub ao Easypanel.

### Passo 4: Configurar o Build
1.  O Easypanel deve detectar automaticamente o `Dockerfile` na raiz do projeto.
2.  **Build Type**: Dockerfile.
3.  **Build Path**: `/Dockerfile` (padrão).

### Passo 5: Configurar Portas e Ambiente
1.  Vá para a aba **Environment**.
2.  A aplicação está configurada para usar a porta definida na variável `PORT` ou `8000` por padrão.
3.  No Easypanel, verifique a configuração **HTTP Port** (ou App Service Port). Defina como `8000`.
4.  Clique em **Save** e depois em **Deploy**.

### Passo 6: Obter a URL
Após o deploy finalizar (pode levar alguns minutos na primeira vez para baixar o navegador), o Easypanel gerará uma URL pública para sua API (ex: `https://rpa-mariana.seu-dominio.com`).

---

## 🔗 Integração com n8n

Para usar este RPA em um fluxo do n8n:

1.  Adicione um node **HTTP Request**.
2.  **Method**: `POST`.
3.  **URL**: A URL gerada pelo Easypanel + `/consultar` (ex: `https://rpa-mariana.seu-dominio.com/consultar`).
4.  **Send Body**: Ative esta opção.
5.  **Body Content Type**: JSON.
6.  **JSON**:
    ```json
    {
      "cpf": "{{ $json.cpf }}"
    }
    ```
    *(Assumindo que o CPF vem de um node anterior)*.

7.  Execute o node e verifique a saída (`result`: "descarte" ou "pesquisar").
