-- Diagnóstico de qualidade dos dados extraídos (tabelas `laudos` e
-- `laudos_amostras`).
--
-- Jeito rápido: rodar `python checar_qualidade_dados.py`, que executa
-- TODAS as consultas daqui de uma vez e salva um relatório único em
-- logs/qualidade_<data>.txt.
--
-- Jeito manual: colar cada bloco separadamente no Adminer (aba "Comando
-- SQL") - alguns clientes só mostram o resultado da ÚLTIMA consulta
-- quando várias são coladas juntas.
--
-- A ideia: a extração quase nunca dá erro visível. Quando uma leitura
-- falha, a linha grava normal, só que com campo vazio, zero ou texto
-- contaminado por pedaço de outra coluna do PDF. As consultas abaixo
-- caçam exatamente essas linhas.
--
-- Formato: cada consulta começa com "-- @@ <título>", seguido de linhas
-- "--" de comentário (que viram a nota "Esperado" no relatório). O
-- checar_qualidade_dados.py depende desse formato pra separar os blocos.


-- ===========================================================
-- Parte 1 - tabela laudos
-- ===========================================================

-- @@ Panorama dos laudos: campos vazios por modelo
-- Esperado: tudo perto de zero, menos idade_zero (imóvel novo tem idade
-- 0 de verdade) e sem_terreno (só casa/lote têm área de terreno).
SELECT modelo_usado,
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE codigo_laudo IS NULL)      AS sem_codigo,
  COUNT(*) FILTER (WHERE numero_proposta = '')      AS sem_proposta,
  COUNT(*) FILTER (WHERE endereco = '')             AS sem_endereco,
  COUNT(*) FILTER (WHERE municipio = '')            AS sem_municipio,
  COUNT(*) FILTER (WHERE tipo_imovel = '')          AS sem_tipo,
  COUNT(*) FILTER (WHERE valor_mercado = 0)         AS valor_zero,
  COUNT(*) FILTER (WHERE area_privativa_m2 = 0
                     AND area_terreno_m2 = 0)       AS sem_area,
  COUNT(*) FILTER (WHERE idade_anos = 0)            AS idade_zero,
  COUNT(*) FILTER (WHERE data_avaliacao IS NULL)    AS sem_data
FROM laudos
GROUP BY modelo_usado;

-- @@ Tipos de imóvel encontrados
-- Esperado: poucas linhas, todas com nome de imóvel de verdade
-- (Apartamento, Casa, Terreno - Lote...). Valor com número no meio ou
-- frase solta = leitura pegou a coluna errada.
SELECT tipo_imovel, COUNT(*) AS qtd
FROM laudos GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

-- @@ Padrões de acabamento encontrados
-- Esperado: Normal, Alto, Médio, Baixo, Normal-alto, Simples...
SELECT padrao_acabamento, COUNT(*) AS qtd
FROM laudos GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

-- @@ Estados de conservação encontrados
-- Esperado: Bom, Regular, Nova|Regular, Ótimo, Ruim...
SELECT estado_conservacao, COUNT(*) AS qtd
FROM laudos GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

-- @@ Metodologias encontradas
-- Esperado: Comparativo direto de mercado, AVM, Método Evolutivo.
SELECT metodologia, COUNT(*) AS qtd
FROM laudos GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

-- @@ UFs encontradas
-- Esperado: só siglas de 2 letras de estado brasileiro.
SELECT uf, COUNT(*) AS qtd
FROM laudos GROUP BY 1 ORDER BY 2 DESC LIMIT 35;

-- @@ Código de laudo duplicado (dois PDFs com o mesmo código)
-- Esperado: nenhuma linha.
SELECT codigo_laudo, COUNT(*) AS qtd, STRING_AGG(path, ', ') AS arquivos
FROM laudos
GROUP BY codigo_laudo HAVING COUNT(*) > 1 LIMIT 15;

-- @@ Datas fora do padrão dd/mm/aaaa ou de ano improvável
-- Esperado: nenhuma linha.
SELECT path, codigo_laudo, data_avaliacao
FROM laudos
WHERE data_avaliacao IS NOT NULL
  AND (data_avaliacao !~ '^\d{2}/\d{2}/\d{4}$'
       OR RIGHT(data_avaliacao, 4) NOT BETWEEN '2023' AND '2026')
LIMIT 15;

-- @@ Quantos laudos com venda forçada fora de 50-90% da avaliação
-- Esperado: número baixo. A regra do Banco B é fator 0,70.
SELECT COUNT(*) AS laudos_fora_da_faixa
FROM laudos
WHERE valor_mercado > 0 AND valor_venda_forcada > 0
  AND valor_venda_forcada / valor_mercado NOT BETWEEN 0.5 AND 0.9;

-- @@ Exemplos de venda forçada fora da faixa
SELECT path, modelo_usado, valor_mercado, valor_venda_forcada,
       ROUND(valor_venda_forcada / NULLIF(valor_mercado,0), 3) AS razao
FROM laudos
WHERE valor_mercado > 0 AND valor_venda_forcada > 0
  AND valor_venda_forcada / valor_mercado NOT BETWEEN 0.5 AND 0.9
ORDER BY razao LIMIT 15;

-- @@ Quantos laudos com valor por m² que não bate com valor / área
-- Esperado: número baixo (tolerância de 2%). O laudo pode arredondar,
-- mas divergência grande significa que a área ou o valor saiu errado.
SELECT COUNT(*) AS laudos_com_unitario_incoerente
FROM laudos
WHERE valor_unitario_m2 > 0
  AND ABS(valor_unitario_m2 - valor_mercado / NULLIF(COALESCE(NULLIF(area_privativa_m2,0),
          NULLIF(area_terreno_m2,0)),0)) > valor_unitario_m2 * 0.02;

-- @@ Exemplos de valor por m² incoerente
SELECT path, tipo_imovel, valor_mercado, area_privativa_m2, area_terreno_m2,
       valor_unitario_m2,
       ROUND(valor_mercado / NULLIF(COALESCE(NULLIF(area_privativa_m2,0),
                                             NULLIF(area_terreno_m2,0)),0), 2) AS esperado
FROM laudos
WHERE valor_unitario_m2 > 0
  AND ABS(valor_unitario_m2 - valor_mercado / NULLIF(COALESCE(NULLIF(area_privativa_m2,0),
          NULLIF(area_terreno_m2,0)),0)) > valor_unitario_m2 * 0.02
LIMIT 15;

-- @@ Quantos laudos com valor, área, idade ou cômodos fora da realidade
-- Esperado: número baixo.
SELECT
  COUNT(*) FILTER (WHERE valor_mercado > 0 AND valor_mercado < 10000) AS valor_baixo_demais,
  COUNT(*) FILTER (WHERE valor_mercado > 50000000)                    AS valor_alto_demais,
  COUNT(*) FILTER (WHERE area_privativa_m2 > 10000)                   AS area_priv_absurda,
  COUNT(*) FILTER (WHERE area_terreno_m2 > 100000)                    AS area_terreno_absurda,
  COUNT(*) FILTER (WHERE idade_anos > 100)                            AS idade_absurda,
  COUNT(*) FILTER (WHERE quartos > 15)                                AS quartos_demais,
  COUNT(*) FILTER (WHERE banheiros > 15)                              AS banheiros_demais,
  COUNT(*) FILTER (WHERE vagas > 30)                                  AS vagas_demais
FROM laudos;

-- @@ Exemplos de laudos com número fora da realidade
SELECT path, tipo_imovel, valor_mercado, area_privativa_m2, area_terreno_m2,
       idade_anos, quartos, banheiros, vagas
FROM laudos
WHERE (valor_mercado > 0 AND valor_mercado < 10000) OR valor_mercado > 50000000
   OR area_privativa_m2 > 10000 OR area_terreno_m2 > 100000
   OR idade_anos > 100 OR quartos > 15 OR banheiros > 15 OR vagas > 30
ORDER BY valor_mercado LIMIT 15;

-- @@ 12 laudos aleatórios pra conferir contra o PDF
-- Abra o PDF de `path` em data/laudos e compare campo a campo.
SELECT path, modelo_usado, numero_proposta, tipo_imovel, municipio, uf,
       area_privativa_m2, area_terreno_m2, quartos, banheiros, vagas,
       idade_anos, padrao_acabamento, estado_conservacao,
       valor_mercado, valor_venda_forcada, valor_unitario_m2, data_avaliacao
FROM laudos ORDER BY random() LIMIT 12;


-- ===========================================================
-- Parte 2 - tabela laudos_amostras
-- ===========================================================

-- @@ Panorama das amostras: campos vazios
-- Esperado: sem_endereco/sem_tipo/valor_zero perto de zero. sem_url é
-- alto de propósito: o modelo digital não traz URL de anúncio.
SELECT COUNT(*) AS total,
  COUNT(*) FILTER (WHERE endereco = '')          AS sem_endereco,
  COUNT(*) FILTER (WHERE tipo_imovel = '')       AS sem_tipo,
  COUNT(*) FILTER (WHERE valor = 0)              AS valor_zero,
  COUNT(*) FILTER (WHERE area_privativa_m2 = 0
                     AND area_terreno_m2 = 0)    AS sem_area,
  COUNT(*) FILTER (WHERE padrao_acabamento = '') AS sem_padrao,
  COUNT(*) FILTER (WHERE estado_conservacao = '')AS sem_estado,
  COUNT(*) FILTER (WHERE url = '')               AS sem_url
FROM laudos_amostras;

-- @@ Colunas vazias das amostras: é erro ou o campo não existe naquele laudo?
-- Cada modelo de laudo traz campos diferentes nas amostras: o físico tem
-- URL do anúncio; o digital (AVM) tem cidade e UF; terreno não tem
-- idade, padrão nem estado. Coluna vazia nesses casos é o esperado.
-- Esperado: com_url ~ amostras só no físico, com_uf ~ amostras só no
-- digital, com_padrao ~ 0 em terreno.
-- Qualquer número fora desse padrão é erro de verdade.
SELECT l.modelo_usado,
       CASE WHEN a.tipo_imovel ILIKE 'Terreno%' THEN 'terreno' ELSE 'construído' END AS tipo,
       COUNT(*)                                          AS amostras,
       COUNT(*) FILTER (WHERE a.url <> '')               AS com_url,
       COUNT(*) FILTER (WHERE a.uf <> '')                AS com_uf,
       COUNT(*) FILTER (WHERE a.padrao_acabamento <> '') AS com_padrao,
       COUNT(*) FILTER (WHERE a.idade_anos > 0)          AS com_idade
FROM laudos_amostras a
JOIN laudos l ON l.codigo_laudo = a.codigo_laudo
GROUP BY 1, 2 ORDER BY 1, 2;

-- @@ Laudos sem nenhuma amostra
-- Esperado: número baixo (laudo que realmente não tem seção de amostras).
SELECT COUNT(*) AS laudos_sem_amostra
FROM laudos l
WHERE NOT EXISTS (SELECT 1 FROM laudos_amostras a WHERE a.codigo_laudo = l.codigo_laudo);

-- @@ Quantas amostras cada laudo tem
-- Esperado: a esmagadora maioria com 5.
SELECT qtd_amostras, COUNT(*) AS laudos
FROM (SELECT codigo_laudo, COUNT(*) AS qtd_amostras
      FROM laudos_amostras GROUP BY codigo_laudo) t
GROUP BY 1 ORDER BY 1;

-- @@ Amostras com texto contaminado por número
-- Era o bug do padrão virar "4 Regular 60 6,67 0,056 20" (a tabela de
-- depreciação sendo lida como se fosse parte da amostra).
-- Esperado: nenhuma linha.
SELECT path, numero_amostra, tipo_imovel, padrao_acabamento, estado_conservacao
FROM laudos_amostras
WHERE padrao_acabamento ~ '[0-9]'
   OR estado_conservacao ~ '[0-9],'
   OR LENGTH(padrao_acabamento) > 25
   OR LENGTH(estado_conservacao) > 25
LIMIT 15;

-- @@ Tipos de imóvel das amostras
SELECT tipo_imovel, COUNT(*) AS qtd
FROM laudos_amostras GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

-- @@ Padrões de acabamento das amostras
SELECT padrao_acabamento, COUNT(*) AS qtd
FROM laudos_amostras GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

-- @@ Estados de conservação das amostras
SELECT estado_conservacao, COUNT(*) AS qtd
FROM laudos_amostras GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

-- @@ Quantas amostras com valor por m² que não bate com valor / área
-- Esperado: número baixo (tolerância de 2%).
SELECT COUNT(*) AS amostras_com_unitario_incoerente
FROM laudos_amostras
WHERE valor_unitario_m2 > 0
  AND ABS(valor_unitario_m2 - valor / NULLIF(COALESCE(NULLIF(area_privativa_m2,0),
          NULLIF(area_terreno_m2,0)),0)) > valor_unitario_m2 * 0.02;

-- @@ Exemplos de valor por m² incoerente nas amostras
SELECT path, numero_amostra, tipo_imovel, valor, area_privativa_m2,
       area_terreno_m2, valor_unitario_m2
FROM laudos_amostras
WHERE valor_unitario_m2 > 0
  AND ABS(valor_unitario_m2 - valor / NULLIF(COALESCE(NULLIF(area_privativa_m2,0),
          NULLIF(area_terreno_m2,0)),0)) > valor_unitario_m2 * 0.02
LIMIT 15;

-- @@ Quantas amostras com número fora da realidade
SELECT
  COUNT(*) FILTER (WHERE valor > 0 AND valor < 10000) AS valor_baixo_demais,
  COUNT(*) FILTER (WHERE valor > 50000000)            AS valor_alto_demais,
  COUNT(*) FILTER (WHERE area_privativa_m2 > 10000)   AS area_absurda,
  COUNT(*) FILTER (WHERE idade_anos > 100)            AS idade_absurda,
  COUNT(*) FILTER (WHERE quartos > 15)                AS quartos_demais,
  COUNT(*) FILTER (WHERE banheiros > 15)              AS banheiros_demais,
  COUNT(*) FILTER (WHERE vagas > 30)                  AS vagas_demais
FROM laudos_amostras;

-- @@ Exemplos de amostras com número fora da realidade
SELECT path, numero_amostra, tipo_imovel, valor, area_privativa_m2,
       idade_anos, quartos, banheiros, vagas
FROM laudos_amostras
WHERE (valor > 0 AND valor < 10000) OR valor > 50000000
   OR area_privativa_m2 > 10000 OR idade_anos > 100
   OR quartos > 15 OR banheiros > 15 OR vagas > 30
ORDER BY valor LIMIT 15;

-- @@ Quantos laudos cuja avaliação destoa das próprias amostras
-- O valor do m² avaliado deveria ficar perto da média das amostras que o
-- próprio laudo usou. Fora da faixa 0,4x-2,5x, ou o laudo ou a extração
-- está errada. Esperado: número baixo.
SELECT COUNT(*) AS laudos_destoando
FROM (
  SELECT l.codigo_laudo
  FROM laudos l
  JOIN laudos_amostras a ON a.codigo_laudo = l.codigo_laudo
  WHERE l.valor_unitario_m2 > 0 AND a.valor_unitario_m2 > 0
  GROUP BY l.codigo_laudo, l.valor_unitario_m2
  HAVING l.valor_unitario_m2 / NULLIF(AVG(a.valor_unitario_m2),0) NOT BETWEEN 0.4 AND 2.5
) t;

-- @@ Exemplos de laudo que destoa das próprias amostras
SELECT l.path, l.tipo_imovel, l.valor_unitario_m2 AS unit_laudo,
       ROUND(AVG(a.valor_unitario_m2), 2) AS unit_media_amostras,
       ROUND(l.valor_unitario_m2 / NULLIF(AVG(a.valor_unitario_m2),0), 2) AS razao
FROM laudos l
JOIN laudos_amostras a ON a.codigo_laudo = l.codigo_laudo
WHERE l.valor_unitario_m2 > 0 AND a.valor_unitario_m2 > 0
GROUP BY l.path, l.tipo_imovel, l.valor_unitario_m2
HAVING l.valor_unitario_m2 / NULLIF(AVG(a.valor_unitario_m2),0) NOT BETWEEN 0.4 AND 2.5
ORDER BY razao LIMIT 15;

-- @@ Amostras de um laudo aleatório, pra conferir contra o PDF
SELECT numero_amostra, tipo_imovel, endereco, cidade, uf, quartos, banheiros,
       vagas, area_privativa_m2, area_terreno_m2, valor, valor_unitario_m2,
       idade_anos, padrao_acabamento, estado_conservacao, url
FROM laudos_amostras
WHERE codigo_laudo = (SELECT codigo_laudo FROM laudos_amostras ORDER BY random() LIMIT 1)
ORDER BY numero_amostra;
