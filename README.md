# Foco

App de foco para Windows feito como *commitment device* (dispositivo de
compromisso): ele resiste ao **impulso** de desistir, mas **nunca** ao dono da
máquina. Você sempre tem uma saída.

## O que ele faz

Durante uma sessão de foco:

- **Timer / Pomodoro** — ciclos de foco e descanso configuráveis.
- **Tela de foco** — janela em tela cheia e sempre no topo com o tempo restante.
- **Bloqueio de sites** — adiciona domínios distrativos ao arquivo `hosts`
  (requer rodar como administrador) e os remove ao fim da sessão.
- **Bloqueio de apps** (opcional, desligado por padrão) — encerra processos de
  uma lista durante o foco. ⚠️ Pode causar perda de trabalho não salvo.
- **Reinício automático** — se a janela for fechada **durante** a sessão, um
  *watchdog* leve a reabre.
- **Registro de sessões** — grava cada sessão em um CSV.

## As três válvulas de escape (de propósito)

Este app foi desenhado para **não** virar malware de persistência. Você, como
administrador, sempre consegue parar:

1. **Frase + atraso** — digite a frase-compromisso e espere o tempo de atraso
   para encerrar antes da hora.
2. **Gerenciador de Tarefas** — encerrar `Foco.exe` e o processo *watchdog* é
   uma saída deliberada e continua funcionando.
3. **Desinstalação normal** — fora de uma sessão ativa, basta apagar a pasta.

O app **não** se instala como serviço, **não** entra na inicialização do
Windows, **não** sobrevive a um reboot além da sessão e **não** bloqueia o
Gerenciador de Tarefas nem ferramentas de administração. Essas ausências são
intencionais.

## Configuração

Na primeira execução é criado `%APPDATA%\Foco\config.json`. Veja
`config.example.json` para todos os campos:

| Campo | Significado |
|---|---|
| `focus_minutes` | duração de cada bloco de foco |
| `break_minutes` | duração do descanso |
| `cycles` | quantos blocos de foco por sessão |
| `exit_delay_seconds` | atraso (atrito) para sair antes da hora |
| `passphrase` | frase longa exigida para encerrar antes |
| `auto_restart` | reabrir a janela se fechada durante a sessão |
| `block_sites` / `blocked_sites` | bloqueio via `hosts` |
| `block_apps` / `blocked_apps` | encerrar processos (cuidado) |

## Rodar a partir do código (teste rápido)

Requer Python 3.10+ no Windows (o `tkinter` já vem incluso):

```
python foco.py
```

Para o bloqueio de sites funcionar, rode o terminal **como administrador**.

## Gerar o executável (Foco.exe)

No Windows, com Python instalado:

```
pip install -r requirements.txt
pyinstaller --onefile --noconsole --name Foco foco.py
```

O executável ficará em `dist\Foco.exe`. Para que o bloqueio de sites funcione,
clique com o botão direito em `Foco.exe` → **Executar como administrador**
(ou crie um atalho com essa opção marcada em Propriedades → Compatibilidade).

## Glossário

- **Commitment device**: estratégia em que você se compromete antecipadamente
  com uma ação, criando atrito para não voltar atrás por impulso.
- **Watchdog** (cão de guarda): processo leve cuja única função é vigiar e
  reabrir a janela de foco se ela for fechada durante a sessão.
- **Arquivo `hosts`**: arquivo do sistema que mapeia nomes de domínio para
  endereços IP; apontar um site para `127.0.0.1` faz o navegador não alcançá-lo.
