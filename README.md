# pipeline-laudos-banco-b

[![Testes](https://github.com/rafaelbertochi1/pipeline-laudos-banco-b/actions/workflows/testes.yml/badge.svg)](https://github.com/rafaelbertochi1/pipeline-laudos-banco-b/actions/workflows/testes.yml)

> **Sobre este repositório.** Versão de portfólio de um projeto real, desenvolvido em
> ambiente corporativo em 2026. Os nomes da empresa, dos sistemas e dos bancos, as URLs
> e os dados de exemplo foram trocados por nomes genéricos ("Central de Gestão",
> "Plataforma", "Banco A", "Banco B"), então o código não roda contra os endereços de
> exemplo. O histórico de commits é novo e resumido; o caminho real do projeto está em
> [HISTORICO.md](HISTORICO.md).

Pipeline **Python + Docker** para baixar laudos de avaliação de imóveis (Plataforma / Banco B) e gravar os dados extraídos num banco PostgreSQL. Não é um projeto Node — não existe `package.json`.

Todo o código fica dentro da pasta [`Backend l Script Extração Laudos/`](./Backend%20l%20Script%20Extra%C3%A7%C3%A3o%20Laudos/).

## Resumo rápido

```powershell
# 1. clonar e entrar na pasta do projeto
git clone https://github.com/rafaelbertochi1/pipeline-laudos-banco-b.git
cd "pipeline-laudos-banco-b\Backend l Script Extração Laudos"

# 2. ambiente virtual + dependências Python
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 3. subir Postgres + Adminer via Docker
docker compose up -d

# 4. credenciais do Plataforma (uma vez por computador - ver seção 8;
#    pule se já configurou pro robô robo-cadastro-banco-b)

# 5. pipeline completo (download + extração), sem tela
python rodar_pipeline.py
```

Se você já manja de Python/Docker, isso é tudo que precisa. Os detalhes de cada passo estão abaixo — vale ler pelo menos a seção de [Segurança](#o-que-nunca-commitar) antes de rodar.

## Diagrama da pipeline

O desenho do fluxo (etapas, validação, onde cada etapa lê e grava) está em [`docs/pipeline_laudos_banco_b.drawio`](./docs/pipeline_laudos_banco_b.drawio). Pra abrir: arraste o arquivo pra dentro de [app.diagrams.net](https://app.diagrams.net) (ou abra no draw.io desktop / extensão "Draw.io Integration" do VS Code). Se mudar alguma etapa, atualize o diagrama junto.

## O que é cada script

| Script | O que faz |
|---|---|
| `plataforma_downloader.py` | Robô com **Playwright** que faz login na Plataforma (cliente Banco B) e baixa os laudos em PDF de um período de datas para `data/laudos/`. |
| `banco_b_extractor.py` | Lê os PDFs com **pdfplumber** e grava no Postgres via **psycopg2**: os campos do laudo (endereço, áreas, valores, infraestrutura do prédio…) na tabela `laudos`, e os imóveis comparativos na tabela `laudos_amostras` (uma linha por amostra). Usa multiprocessing para paralelizar. Laudo de empreendimento inteiro (tipo "Múltiplas Unidades", crédito PJ) não é gravado: o nome do PDF vai pra `data/laudos/_fora_do_banco.json` e ele não é relido. Chama `banco_b_imagens.py` a cada PDF, então já sai com as fotos extraídas também. **Antes de gravar, valida o lote inteiro** com as mesmas checagens do `testar_amostra.py` (`checagens.py`): se alguma passar do limite (ex.: laudos sem endereço - sinal de layout novo), **não grava nada**, mostra o relatório e sai com erro; `$env:VALIDACAO_IGNORAR="1"` grava mesmo assim. |
| `banco_b_imagens.py` | Extrai fotos de cada laudo PDF (com **pymupdf**) e salva em `data/imagens/`: a fachada (`laudo_<id>_img.<ext>`) e **todas as fotos do "RELATÓRIO FOTOGRÁFICO"**, cada uma com um label tirado da legenda no nome do arquivo: `laudo_<id>_<label>.<ext>` pra primeira de cada label, `_<label>_2`, `_3`... pras seguintes (os nomes antigos, `laudo_<id>_quarto.jpg`, continuam valendo). Labels: sala, quarto, banheiro, cozinha, area_servico, varanda, garagem, quintal, escritorio, closet, despensa, corredor, escada, telhado, medidor, e as áreas comuns e de lazer do prédio (salao_festas, playground, academia, piscina, quadra, churrasqueira, portaria, elevador, area_comum), além de entorno, croqui, mapa, numero e fachada (fachadas extras saem como `fachada_2`...). Legenda que não cai em label nenhum vira `outro` com a legenda embutida no nome (`laudo_<id>_outro_1_closet.jpg`); foto sem legenda embaixo vira `sem_legenda`. A lista de labels é `CATEGORIAS_FOTO` no topo do arquivo, e o resumo da execução mostra as legendas mais comuns que viraram `outro`, pra ir crescendo a lista. Foto repetida no mesmo laudo sai uma vez só; página escaneada inteira não conta como foto. Foto de cômodo só existe no laudo físico (páginas do "RELATÓRIO FOTOGRÁFICO"); o digital/AVM não tem vistoria, então dele sai só a fachada. Laudo físico sem nenhuma legenda "Fachada" usa a foto grande da página 1. Roda sozinho (`python banco_b_imagens.py`, em paralelo, um processo por núcleo) ou é chamado pelo `banco_b_extractor.py` em toda extração nova. Nunca regrava nem renomeia o que já está na pasta; foto trocada por mudança de regra vai pra `data/imagens/_removidas/`. PDFs já lidos ficam em `data/imagens/_pdfs_verificados.json` e não são reabertos; **na primeira execução depois desta versão todos os PDFs são reabertos uma vez** (horas na base inteira) pra tirar as fotos que faltavam. |
| `banco_b_geocodificar.py` | Calcula latitude/longitude pelo endereço **só dos laudos que não trazem a coordenada no PDF** (o digital até meados de 2025 não imprime). Tenta o OpenStreetMap pelo número, depois pela rua, depois o CEP (AwesomeAPI) e por último a cidade. Testado em 200 laudos com coordenada real: erro típico de ~180 m, 2 em cada 3 a menos de 500 m, ~8% a mais de 5 km - dá ideia da região, não do imóvel exato. A coluna `origem_coordenada` diz de onde veio: `laudo` (do PDF) ou `calculada_numero` / `calculada_rua` / `calculada_cep` / `calculada_cidade`. Só manda endereço, número, cidade, UF e CEP pros serviços. Guarda cada consulta em `data/laudos/_geocodificacao.json` e nunca repete; a primeira execução na base leva horas (o OpenStreetMap aceita 1 consulta por segundo), as seguintes só consultam os laudos novos. Teste sem gravar: `$env:GEOCODIFICAR_SIMULAR="1"; $env:GEOCODIFICAR_LIMITE="30"`. No `rodar_pipeline.py` roda **em segundo plano**: o pipeline termina e já dá pra começar o próximo ciclo; se ela ainda estiver rodando, a próxima espera ela terminar (nunca duas juntas) e a que está rodando pega os laudos novos antes de sair. Pra esperar ela no próprio pipeline: `$env:COORDENADAS_ESPERAR="1"`. |
| `rodar_pipeline.py` | Orquestra download + extração + imagens em sequência, e deixa as coordenadas rodando em segundo plano e mostra quanto tempo cada etapa levou. **Forma recomendada de rodar tudo.** |
| `rodar_extracao.bat` | Atalho Windows (2 cliques): sobe o Docker, instala o `requirements.txt` e roda só a extração — **não baixa laudos novos**. |
| `testar_amostra.py` | **Teste rápido antes de rodar na base inteira.** Roda extração e imagens numa amostra (60 laudos por padrão, 1/3 deles os baixados mais recentemente) sem gravar nada no banco nem em `data/imagens/`, e aponta problema conhecido: campo vazio ou absurdo, modelo trocado, texto de outra coluna, foto de cômodo igual à da fachada, mais a conferência contra a segunda fonte do PDF. Termina com um veredito ("pode rodar" / "não rode ainda"). Relatório em `logs/teste_rapido_<data>.txt`. |
| `checar_qualidade_dados.py` | Roda de uma vez todas as consultas de qualidade do `.sql` e salva um relatório único em `logs/qualidade_<data>.txt` — campos vazios, valores incoerentes, texto contaminado, outliers e uma amostra aleatória pra conferir contra o PDF. **Forma recomendada de checar os dados depois de uma extração.** |
| `conferir_extracao.py` | Mede a **taxa de acerto** da extração: sorteia laudos (500 por padrão) e compara cada campo do banco com uma segunda fonte dentro do próprio PDF — as tabelas de cálculo avaliatório, a "AVALIAÇÃO FINAL", a linha "Avaliando", o formulário da página 3 (digital) e campos repetidos entre capa e questionário (físico) —, lidas por um código independente do extrator. Cobre ~20 campos de `laudos` e 7 de `laudos_amostras`. Separa **erro de extração** de **laudo inconsistente** (quando o laudo imprime um valor num lugar e outro valor em outro). Relatório em `logs/conferencia_<data>.txt`. |
| `checar_qualidade_dados.sql` | As consultas de qualidade (lidas pelo script acima), pra quem preferir rodar direto no Postgres via Adminer ou psql. |
| `reordenar_colunas_laudos.py` | Muda a posição de uma coluna da tabela `laudos` no Adminer (ex.: `python reordenar_colunas_laudos.py origem_coordenada coordenadas`). Recria a tabela lendo as colunas atuais do banco, numa transação só, e confere que o conteúdo ficou idêntico antes de confirmar. Nenhum script depende da ordem das colunas. Faça um backup antes: `docker exec postgres_pdf pg_dump -U postgres -Fc testdb > backup_testdb.dump` (arquivos `.dump` nunca vão pro GitHub). |

## O que instalar

| Ferramenta | Versão | Para quê |
|---|---|---|
| Git | mais recente | clonar o repositório |
| Python | 3.11 ou 3.12 (64-bit) | rodar os scripts `.py` |
| Docker Desktop | mais recente, com WSL2 no Windows | subir Postgres 15 + Adminer |

## Passo a passo completo

### 1. Instalar o Git
Baixe em `git-scm.com`, instale com as opções padrão e confirme:
```powershell
git --version
```

### 2. Clonar o repositório
```powershell
git clone https://github.com/rafaelbertochi1/pipeline-laudos-banco-b.git
cd "pipeline-laudos-banco-b\Backend l Script Extração Laudos"
```
Repare no nome da pasta: `Backend l Script Extração Laudos` (com espaços e um "l" minúsculo) — é assim mesmo.

### 3. Instalar o Python
Baixe o instalador 3.11 ou 3.12 (64-bit) em `python.org`. Na primeira tela do instalador, marque **"Add python.exe to PATH"**.
```powershell
python --version
```

### 4. Criar o ambiente virtual e instalar os pacotes
O `requirements.txt` fica dentro de `Backend l Script Extração Laudos/`:
```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
| Pacote | Para quê |
|---|---|
| `playwright` | automação do navegador (`plataforma_downloader.py`) |
| `pdfplumber` | lê os PDFs dos laudos (`banco_b_extractor.py`) |
| `psycopg2-binary` | grava no Postgres (`banco_b_extractor.py`) |
| `pymupdf` (`fitz`) | extrai as fotos dos PDFs (`banco_b_imagens.py`) |

### 5. Instalar o navegador do Playwright
```powershell
playwright install chromium
```

### 6. Instalar o Docker Desktop e subir o banco
Instale o Docker Desktop (Windows: com backend WSL2 ativado) e deixe-o aberto. Depois, na pasta do projeto:
```powershell
docker compose up -d
```
Isso sobe dois containers definidos no `docker-compose.yml`:

| Container | Porta | Credenciais |
|---|---|---|
| `postgres_pdf` | 5432 | `postgres` / `postgres` / `testdb` |
| `adminer_pdf` | 8080 | abrir `localhost:8080` no navegador |

### 7. Variáveis de ambiente (opcional na maioria dos casos)
Os valores padrão do código já batem com o `docker-compose.yml`, então localmente não precisa configurar nada. Só defina se for apontar para um banco diferente ou mudar o comportamento do robô:

| Variável | Padrão no código | Onde é usada |
|---|---|---|
| `PGURL` | `127.0.0.1` | `banco_b_extractor.py` |
| `PGNAME` | `testdb` | idem |
| `PGUSR` | `postgres` | idem |
| `PGPASS` | `postgres` | idem |
| `PGPORT` | `5432` | idem |
| `HEADLESS` | `1` (true) | `plataforma_downloader.py` — `0` (ou `false`) abre o navegador visível |
| `PARALELISMO` | `6` | `plataforma_downloader.py` — abas simultâneas na coleta e no download (máx. 12). Se a Plataforma ficar lenta ou devolver erro com mais abas, volte pro padrão |
| `RECHECAR_SEM_LAUDO_DIAS` | `7` | `plataforma_downloader.py` — laudo checado "sem laudo publicado" só é aberto de novo depois desses dias (`0` = checar sempre). A lista fica em `data/laudos/_sem_laudo_publicado.json` |
| `DOWNLOAD_API` | `1` | `plataforma_downloader.py` — baixa o PDF direto pela API da Plataforma (sem abrir o modal de cada laudo), reaproveitando os cabeçalhos das chamadas que o próprio site faz. O que não der (código não achado nos dados da grade, erro na API) vai pela tela como antes. `0` = só pela tela. No fim, o resumo mostra quantos foram por cada caminho e quanto a Plataforma levou em cada chamada |
| `DOWNLOADS_SIMULTANEOS` | `12` | `plataforma_downloader.py` — quantos PDFs baixar pela API ao mesmo tempo (máx. 24), numa fila única repartida entre as abas. Se a Plataforma começar a devolver erro, diminua (ex.: `$env:DOWNLOADS_SIMULTANEOS="6"`) |
| `DIAS_POR_LOTE` | `31` | `plataforma_downloader.py` — período grande é quebrado em lotes de até esse tanto de dias, um de cada vez (coleta + download), conferindo a sessão antes de cada lote. Com lotes de um mês cada aba filtra ~5 dias, o tamanho que já rodou em produção; filtro largo perdia laudos. `0` = período inteiro de uma vez, como antes |
| `PERIODO_INICIO` / `PERIODO_FIM` | — | `plataforma_downloader.py` — datas em dd/mm/aaaa; com as duas definidas o robô não pergunta nada no terminal (ex.: `$env:PERIODO_INICIO="01/01/2024"; $env:PERIODO_FIM="31/12/2024"`). A data é validada antes de começar |
| `IMAGENS_PROCESSOS` | nº de núcleos | `banco_b_imagens.py` — quantos PDFs processar ao mesmo tempo. Use um número menor (ex.: `$env:IMAGENS_PROCESSOS="4"`) pra deixar o PC mais livre enquanto roda |
| `PLATAFORMA_USUARIO` / `PLATAFORMA_SENHA` | — | `plataforma_downloader.py` — login automático quando a sessão expira (seção 8) |
| `PLATAFORMA_CHAVE_AUTENTICADOR` | — | `plataforma_downloader.py` — responde sozinho a verificação em duas etapas (seção 8) |
| `FORCAR_REPROCESSAR` | `0` | `banco_b_extractor.py` — `1` reprocessa PDFs já gravados |
| `REPROCESSAR_LISTA` | — | `banco_b_extractor.py` — caminho de um `.txt` com um código de laudo (ou nome do PDF) por linha: relê e regrava só esses, pra corrigir os afetados por um bug sem reprocessar a base inteira |
| `REPROCESSAR_SEM_ENDERECO` | `0` | `banco_b_extractor.py` — `1` relê só os laudos gravados sem endereço/município (capa de 2023 e começo de 2024, antes lida errado), em vez da base inteira |

```powershell
$env:HEADLESS="0"
python plataforma_downloader.py
```

### 8. Login no Plataforma
O robô guarda a sessão em `sessao_plataforma.json` (raiz da pasta, **nunca vai pro Git**). Quando ela expira, ele **loga sozinho**: preenche usuário e senha e responde a verificação em duas etapas (MFA) com o código do App Autenticador. É a mesma lógica e são as **mesmas variáveis** do robô `robo-cadastro-banco-b`. Se você já configurou lá, neste computador, não precisa fazer nada aqui.

Configurar uma vez por computador (ficam no seu usuário do Windows, nunca no código):
```powershell
[Environment]::SetEnvironmentVariable("PLATAFORMA_USUARIO", "voce@exemplo.com", "User")
[Environment]::SetEnvironmentVariable("PLATAFORMA_SENHA", "suasenha", "User")
```
A chave do App Autenticador (`PLATAFORMA_CHAVE_AUTENTICADOR`) se configura com o `configurar_autenticador.py` do repositório `robo-cadastro-banco-b` (README de lá, seção Login): ele lê o QR code da Plataforma e só salva se o código bater com o do celular. A Plataforma pede MFA em toda sessão nova, sem opção de "lembrar este dispositivo", então sem essa chave o login automático para na verificação.

Depois de configurar, **feche e reabra o VS Code inteiro** (variável nova não aparece em terminal já aberto). Pra conferir sem mostrar os valores:
```powershell
"USUARIO: $([bool]$env:PLATAFORMA_USUARIO)  SENHA: $([bool]$env:PLATAFORMA_SENHA)  CHAVE: $([bool]$env:PLATAFORMA_CHAVE_AUTENTICADOR)"
```
Tem que aparecer `True` nos três.

Sem as variáveis, ou se o login automático falhar, dá pra logar na mão com a janela visível:
```powershell
$env:HEADLESS="0"
python plataforma_downloader.py
$env:HEADLESS=""
```
O terminal avisa quando é sua vez de agir ("A AÇÃO É SUA AGORA"). Quando o login falha de verdade, o robô salva um print da tela em `logs/falha_login_plataforma_<motivo>_<data>.png`.

### 9. Rodar o pipeline
| Como | O que faz |
|---|---|
| `python rodar_pipeline.py` | download + extração + imagens em sequência, com relatório de tempo. **Recomendado.** Pode dar um período de meses ou anos de uma vez: o downloader vai em lotes de até 31 dias e, se a sessão vencer no meio e não renovar, o resumo diz de que data continuar. |
| 2 cliques em `rodar_extracao.bat` | sobe o Docker e roda *só a extração* — não baixa laudos novos nem instala Playwright/PyMuPDF. Use depois de já ter PDFs em `data/laudos/`. |
| manual | `docker compose up -d` → `python plataforma_downloader.py` → `python banco_b_extractor.py` |

> ⚠️ `rodar_extracao.bat` sozinho **não baixa laudos novos**. Se `data/laudos/` estiver vazia, rode o downloader antes (ou use `rodar_pipeline.py`).

### 10. Ver e checar os dados
Abra `http://localhost:8080` (Adminer) → sistema PostgreSQL → servidor `postgres`, usuário/senha `postgres`, base `testdb`. Duas tabelas no schema `public`:

| Tabela | O que guarda |
|---|---|
| `laudos` | uma linha por laudo — número do pedido, endereço completo (logradouro, número, complemento, bairro, município, UF, CEP), matrícula, tipo, metodologia, áreas, quartos/banheiros/vagas, idade, padrão, conservação, valores (avaliação, venda forçada, R$/m²), coordenadas e três listas separadas por `;`: `infraestrutura` do prédio (Playground, Churrasqueira, Quadra Esportiva…), `infraestrutura_urbana` da região (Água, Energia Elétrica, Esgoto…) e `servicos_publicos` (Ônibus, Escola, Coleta de Lixo…). No laudo digital as duas últimas vêm de checkboxes desenhados no PDF — o script mede se a caixinha está preenchida. |
| `laudos_amostras` | uma linha por imóvel comparativo usado no cálculo — endereço, tipo, quartos/banheiros/vagas, área privativa, valor, valor por m², idade, padrão, conservação, área do terreno (casas e lotes) e URL do anúncio. Ligada ao laudo pelo `codigo_laudo`. Funciona nos dois modelos de laudo: no físico (blocos `AMOSTRA 1`, com URL do anúncio) e no digital/AVM (blocos `Amostra n.0`, renumerados a partir de 1, que trazem também cidade e UF). Os campos que o laudo não traz pra aquele tipo de imóvel ou modelo ficam zerados/vazios (terreno não tem quartos nem idade; apartamento não tem área de terreno; físico não tem cidade/UF; digital não tem URL). O valor por m² é calculado (`valor / área privativa`, ou `/ área do terreno` em lote) quando o laudo não imprime. |

As duas usam chave única (`codigo_laudo` e `codigo_laudo` + `numero_amostra`), então reprocessar um laudo **atualiza** os registros em vez de duplicar.

Os laudos atuais (layout Plataforma, capa com "DADOS DO PEDIDO") são lidos por **posição das colunas** na página, não por regex no texto corrido — a capa e o questionário são grades de rótulo/valor em 2 a 4 colunas, e lidas como texto as colunas se misturam. O modelo é decidido pela presença do questionário da vistoria — o campo `01 - Tipo do Imóvel Avaliado` (físico) ou a ausência dele (digital/AVM). A âncora é o campo e não o título da seção porque o título já mudou de nome ("VISTORIA DO IMÓVEL" nos laudos antigos, "QUESTIONÁRIO" nos novos). Pelo mesmo motivo, as grades da capa aceitam mais de uma versão de rótulo ("N° do Pedido"/"N° da Proposta", com e sem a coluna IPTU). Laudo fora desse layout cai na leitura antiga por texto (`extrair_modelo_fisico` / `extrair_modelo_digital`).

**Antes de rodar na base inteira** (depois de um `git pull` que mudou a extração, antes de reprocessar com `FORCAR_REPROCESSAR`; ou depois de baixar meses novos, antes de extrair):

```powershell
python testar_amostra.py
```

Leva 1–2 minutos e não mexe no banco. Se o veredito no final for "NÃO RODE NA BASE INTEIRA AINDA", mande o relatório (`logs/teste_rapido_*.txt`) antes. Pra uma amostra maior: `$env:TESTE_QTD="150"`.

Para checar a qualidade dos dados depois de uma extração:

```powershell
cd "Backend l Script Extração Laudos"
python checar_qualidade_dados.py
```

Ele roda todas as consultas de `checar_qualidade_dados.sql` de uma vez e salva o resultado em `logs/qualidade_<data>.txt`. Quem preferir pode colar cada consulta do `.sql` na aba "Comando SQL" do Adminer, uma de cada vez.

O relatório de qualidade acha dado **suspeito** (vazio, zerado, fora da realidade). Pra saber se o dado está **certo**, use a conferência:

```powershell
python conferir_extracao.py
```

Ela sorteia 500 laudos e compara o banco com as tabelas de cálculo do próprio PDF, que repetem os números das amostras e do imóvel avaliado. Cada comparação sai como *bate*, *laudo inconsistente* (a extração leu certo; o laudo é que se contradiz) ou *erro de extração*. Pra conferir mais laudos: `$env:CONFERIR_QTD="2000"` (ou `"todos"`, que leva perto de uma hora).

## Testes

Os testes automatizados cobrem as partes que decidem se um dado entra no banco: a conversão dos números, datas, coordenadas e endereços impressos no PDF (`banco_b_extractor.py`) e a validação do lote antes de gravar (`checagens.py` e `validar_lote`). Cada caso vem de um formato que já apareceu de verdade nos laudos — por exemplo, a área `"61.800"`, que antes virava 61.800 m².

```powershell
cd "Backend l Script Extração Laudos"
pip install -r requirements.txt pytest
pytest
```

Não precisam de banco, internet nem PDFs: rodam em menos de um segundo. O GitHub Actions roda a mesma suíte a cada push (selo no topo deste README).

## O que nunca commitar

O `.gitignore` já bloqueia estes caminhos — a lista existe pra você saber *por quê*, e conferir com `git status` antes de qualquer commit:

| Arquivo/pasta | Por que |
|---|---|
| `sessao_plataforma.json` | cookies da sessão de login no Plataforma — equivale a estar logado na conta. |
| `chrome_profile_plataforma/` | perfil antigo do Chrome de uma versão anterior do robô, mesmo motivo. |
| `data/laudos/*.pdf` | os próprios laudos baixados — CPF, nome, endereço e valores de clientes reais. |
| `data/imagens/` | fotos de fachada extraídas dos laudos — mostram o imóvel do cliente, mesma confidencialidade dos PDFs. |
| `logs/` | logs de execução do robô, guardam e-mail e datas usadas nas buscas. |

## Problemas comuns

- **"Nao foi possivel subir o Docker"** — o Docker Desktop precisa estar aberto e com o motor rodando antes de `docker compose up -d`.
- **"A sessão salva expirou e não consegui logar sozinho"** — faltam `PLATAFORMA_USUARIO`/`PLATAFORMA_SENHA` neste terminal, ou estão erradas (seção 8). Se você acabou de configurar, feche e reabra o VS Code.
- **Linhas `[MFA]` e o robô para** — a senha foi aceita, mas falta `PLATAFORMA_CHAVE_AUTENTICADOR` (seção 8). Se aparecer "código recusado", confira o relógio do Windows (Configurações → Hora → Sincronizar agora).
- **Playwright reclama que não achou o navegador** — rode `playwright install chromium` de novo.
- **Adminer não conecta no banco** — no campo "Servidor", use `postgres` (nome do serviço no `docker-compose.yml`), não `localhost`.
