# Histórico do projeto

Este repositório é a versão de portfólio de um projeto real, desenvolvido em
ambiente corporativo entre 25/08/2026 e 02/10/2026. O repositório original é
privado e teve 72 commits de trabalho nesse período.

O histórico de commits que você vê aqui é **novo**: o código foi publicado em
poucos commits, um por componente, depois de anonimizado. Este arquivo conta
como o projeto de fato evoluiu.

## O que foi anonimizado

- Nome da empresa e do seu sistema interno: aparece como "Central de Gestão".
- Sistema de laudos de terceiros: aparece como "Plataforma".
- Bancos clientes: "Banco A" e "Banco B".
- URLs e endpoints: trocados por endereços `example.com`.
- Números de proposta, códigos de inspeção, telefones e endereços que apareciam
  em comentários e exemplos: trocados por valores fictícios.

Por causa disso o código não roda contra os endereços de exemplo. A lógica, a
estrutura e a documentação são as do projeto real.

## Como o projeto evoluiu

### 1. Ponto de partida (25/08 a 27/08)

O projeto partiu de scripts de extração em R já existentes na empresa, que
não fazem parte deste repositório, e de um primeiro extrator em Python.
Nessa fase:

- a tabela de laudos passou a ser criada automaticamente;
- os PDFs de laudo saíram do controle de versão, por serem confidenciais;
- nasceu o robô de download na Plataforma, com seleção automática do cliente
  e execução sem tela;
- foram corrigidos os primeiros erros de extração: número de proposta com
  ponto, data de avaliação, precisão numérica, valor unitário e coordenadas.

### 2. Desempenho e confiabilidade do download (10/09 a 11/09)

- coleta paralela por sub-período e otimização da leitura por página;
- correção de uma perda de cerca de 29% dos downloads, causada pelo filtro de
  período que não era reaplicado;
- correção de um travamento fatal por modal preso na paginação;
- `rodar_pipeline.py`, para executar download e extração num comando só;
- primeiro script de diagnóstico de qualidade dos dados.

### 3. Extração mais rica (11/09 a 22/09)

- área de terreno e valor unitário calculado;
- parser da tabela de unidades dos laudos de múltiplas unidades;
- infraestrutura do prédio, infraestrutura urbana e serviços públicos;
- amostras de mercado do laudo, inclusive no modelo digital;
- leitura do layout novo do laudo e da capa no formato de 2023.

### 4. Imagens (17/09 a 02/10)

A extração de imagens passou por seis versões:

- começou pela foto da fachada, depois uma foto de cada cômodo;
- ganhou processamento em paralelo, com o PDF aberto uma única vez;
- passou a registrar os PDFs já processados, para não refazer trabalho;
- terminou extraindo todas as fotos do relatório fotográfico, nomeadas pela
  legenda, com o lote cerca de 4 vezes mais rápido.

### 5. Medir antes de confiar (22/09 a 29/09)

- relatório de qualidade dos dados num comando só;
- `conferir_extracao.py`, que mede a taxa de acerto comparando cerca de 20
  campos do banco com uma segunda fonte dentro do próprio PDF;
- `testar_amostra.py`, um teste rápido antes de rodar na base inteira;
- validação do lote antes de gravar no banco.

### 6. Login automático e download pela API (24/09 a 01/10)

- login automático na Plataforma, inclusive a verificação em duas etapas;
- download do PDF direto pela API, com a tela como reserva;
- fila única de downloads simultâneos;
- execução em lotes de até 31 dias, com o período por variável de ambiente.

### 7. Coordenadas e fechamento (28/09 a 02/10)

- geocodificação pelo endereço para laudos sem coordenada no PDF, rodando em
  segundo plano no pipeline;
- diagrama do pipeline em draw.io (`docs/`);
- laudos de inspeção de empreendimento inteiro passaram a ficar fora do banco.

## Projetos relacionados

- [pipeline-laudos-banco-a](https://github.com/rafaelbertochi1/pipeline-laudos-banco-a):
  o mesmo pipeline para o Banco A, que tem outro layout de laudo.
- [robo-cadastro-banco-a](https://github.com/rafaelbertochi1/robo-cadastro-banco-a) e
  [robo-cadastro-banco-b](https://github.com/rafaelbertochi1/robo-cadastro-banco-b):
  robôs que levam o status das inspeções para o sistema de gestão.
