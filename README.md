# Bot de comparação PostgreSQL

Compara valores de várias tabelas no banco local e atualiza **somente registros existentes em produção**. Não insere nem exclui registros e não altera a estrutura das tabelas. Você pode configurar uma lista de colunas ou usar `"columns": "*"` para todas as colunas atualizáveis.

Campos locais nulos ou vazios são ignorados, preservando o valor em produção. Isso inclui `NULL`, texto vazio ou composto somente por espaços, listas/arrays vazios e objetos JSON vazios. `false` e `0` são valores válidos e podem atualizar produção. Um valor preenchido no local pode preencher um campo nulo em produção. A regra é aplicada por coluna: outras diferenças no mesmo registro continuam sendo atualizadas.

## Preparação

Requer Python 3.10 ou superior. No PowerShell, dentro desta pasta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Edite `config.json` com os nomes reais das tabelas. O exemplo é ilustrativo: `produtos`, `codigo`, `descricao` e `preco` ainda precisam ser confirmados.

```json
{
  "tables": [
    {
      "schema": "public",
      "table": "produtos",
      "key": ["codigo"],
      "columns": ["descricao", "preco"]
    },
    {
      "schema": "public",
      "table": "precos",
      "key": ["codigo_produto", "codigo_tabela"],
      "columns": ["valor"]
    }
  ]
}
```

`key` identifica o mesmo registro nos dois bancos e pode conter mais de uma coluna. Prefira uma chave de negócio estável, como código de produto. Use `id` somente se você tiver confirmado que ele identifica o mesmo produto nos dois bancos; IDs gerados separadamente podem apontar para produtos diferentes. Chaves nulas ou duplicadas fazem o bot cancelar a operação.

`columns` é a lista de valores autorizados para atualização. Evite incluir colunas que produção mantém independentemente, como estoque, sem decidir explicitamente que os valores locais devem prevalecer. Colunas geradas e identity não podem ser atualizadas. Os tipos das colunas selecionadas devem coincidir nos dois bancos.

Para comparar todas as colunas, use:

```json
{
  "tables": [
    {
      "schema": "public",
      "table": "comparativo_fixas_mvp",
      "key": ["NOME_DA_COLUNA_CODIGO"],
      "columns": "*"
    }
  ]
}
```

Substitua `NOME_DA_COLUNA_CODIGO` pelo nome real da coluna que identifica o produto. `key` recebe nomes de colunas, não valores como `4301771` ou `4570002`. O banco `tabelas_comparativas` é indicado por `dbname` na conexão; `schema` recebe `public` e `table` recebe apenas `comparativo_fixas_mvp`.

O asterisco descobre as colunas no banco e exclui as chaves configuradas, colunas geradas e identity. Todas as demais, incluindo links e datas preenchidas, entram na comparação. Nulos e vazios do local continuam sendo ignorados. Se as listas de colunas dos dois bancos diferirem, o bot cancela para você conferir a estrutura. O relatório registra a lista de colunas efetivamente comparadas; gere outro relatório após mudar a configuração.

## Conexões

Defina as conexões na sessão do PowerShell, sem salvar senhas no projeto. As informações são as mesmas usadas na conexão do DBeaver, mas o bot conecta diretamente ao PostgreSQL. Se você usa túnel SSH, ele precisa estar disponível para o bot também.

```powershell
$env:LOCAL_DSN = 'host=localhost port=5432 dbname=seu_banco_local user=seu_usuario'
$env:PRODUCTION_DSN = 'host=seu_servidor port=5432 dbname=seu_banco_producao user=seu_usuario sslmode=verify-full'
```

Configure senhas com o arquivo `pgpass.conf` do PostgreSQL ou acrescente `password=...` às variáveis de ambiente em uma sessão privada. `verify-full` requer certificado válido e a CA configurada para seu servidor. Não publique credenciais no chat nem no repositório.

O usuário local precisa de SELECT. Para comparar, o usuário de produção também precisa apenas de SELECT. Para aplicar, precisa de UPDATE nas colunas configuradas e permissão para bloquear as tabelas.

## Comparar e revisar

```powershell
.\.venv\Scripts\python.exe sync_bot.py compare --report relatorio-01.json
```

Nenhuma escrita é executada nos bancos. O relatório contém a chave, os valores anteriores e os novos valores de cada registro alterado, além da quantidade de registros ignorados em cada banco. Campos locais nulos ou vazios não geram atualizações. O arquivo contém dados reais: guarde-o com o mesmo cuidado que os dados do banco. Um relatório existente não é sobrescrito; use outro nome. Após atualizar o bot, gere um novo relatório: planos da versão anterior são recusados.

## Aplicar o relatório revisado

Copie o hash impresso pela comparação, depois de revisar o relatório:

```powershell
.\.venv\Scripts\python.exe sync_bot.py apply --report relatorio-01.json --approve COLE_O_HASH_AQUI
```

Após confirmar a transação, o bot gera `relatorio-01.somente-producao.txt` com os registros encontrados em produção e ausentes na origem. Esses registros permanecem intactos. O TXT contém a tabela, a chave e os valores das colunas comparadas; o terminal mostra a quantidade preservada e o caminho do arquivo. Se não houver registros extras, o TXT informa isso. O JSON da comparação também contém essa lista em `production_only_rows`.

Para escolher o nome do TXT:

```powershell
.\.venv\Scripts\python.exe sync_bot.py apply --report relatorio-01.json --approve COLE_O_HASH_AQUI --missing-report registros-preservados.txt
```

O TXT não é sobrescrito. Um arquivo existente ou uma pasta de destino ausente cancela a aplicação antes das escritas. Se a gravação do TXT falhar depois do commit, o bot informa que as alterações já foram confirmadas e orienta consultar a lista no JSON. Guarde o TXT com o mesmo cuidado que os demais relatórios. Gere um novo JSON após atualizar o bot; relatórios antigos não contêm a lista necessária.

O bot lê os dois bancos novamente e cancela se as diferenças, as conexões ou a configuração não corresponderem ao relatório. Alterações em colunas fora da configuração não entram nessa verificação. O relatório não é executado como SQL: os valores usados vêm da nova leitura do banco local. Os nomes de tabela/coluna são escapados e os valores são enviados como parâmetros.

Todas as tabelas configuradas são atualizadas em uma única transação em produção. Erros antes do commit provocam rollback. Durante essa etapa, o bot bloqueia escritas concorrentes nas tabelas configuradas; SELECTs comuns podem continuar. O limite de espera pelo bloqueio é 5 segundos, e cada comando tem limite de 60 segundos. Escolha um horário adequado para o volume das tabelas.

Triggers continuam ativos. O bot confere os valores selecionados ao final e cancela se detectar um resultado diferente do esperado; efeitos em tabelas ou colunas não configuradas não são verificados. Triggers que fazem chamadas externas podem ter efeitos que o rollback do PostgreSQL não desfaz. Se a conexão cair durante o commit, confira o banco e gere uma nova comparação antes de tentar novamente.

Esta primeira versão carrega as colunas selecionadas de todas as linhas em memória. Para tabelas muito grandes, será necessário adaptar a comparação para lotes. RLS e permissões podem limitar quais registros o usuário consegue enxergar: configure o acesso ao conjunto completo que deseja sincronizar.

## Validação

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

Os testes cobrem comparação, chaves compostas, nulos, duplicidades, atualização parametrizada e detecção de resultados inesperados. Não substituem um teste de integração em dois bancos de homologação, que ainda exige suas conexões e tabelas reais.

Referências: [transações do Psycopg](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) e [bloqueios no PostgreSQL](https://www.postgresql.org/docs/current/sql-lock.html).
