# Publicador DANPE · Instagram @somosdanpe

Publica posts, carrosséis, reels e stories no Instagram pela **API oficial da Meta** (Instagram Login), direto do GitHub Actions. Sem Metricool, sem limite mensal, sem computador ligado.

O Claude produz as peças (skill `danpe-instagram`), coloca na fila e o publicador solta cada item no horário.

## Como funciona

```
queue/      itens esperando o horário (um .json por publicação)
published/  itens publicados (com link e id do Instagram)
failed/     itens que falharam 3 vezes (ver last_error)
media/      imagens e vídeos servidos para a API (JPEG)
publisher/  o script
```

- A cada 15 minutos (e logo depois de cada item novo na fila), o fluxo **Publicar no Instagram** roda.
- Tudo que já passou do `publish_at` sai **em ordem de horário**. Se um item falha, a execução para ali, para não embaralhar a ordem das stories, e tenta de novo na próxima rodada.
- PNG e WebP são convertidos para JPEG sozinhos (a API só aceita JPEG) e gravados em `media/<id>/`.
- Limite da Meta: até 100 publicações por 24 h (carrossel conta 1).

## Formato de um item

`queue/2026-11-10T1000-carrossel-motor-01.json`

```json
{
  "id": "2026-11-10-carrossel-motor-01",
  "publish_at": "2026-11-10T10:00:00-03:00",
  "type": "carousel",
  "media": ["media/2026-11-10-motor-01/01.png", "media/2026-11-10-motor-01/02.png"],
  "caption": "Texto da legenda...\n\n#clinicadeestetica"
}
```

| type | media | observações |
|---|---|---|
| `image` | 1 imagem | legenda em `caption` |
| `carousel` | 2 a 10 imagens/vídeos | o 1º define o recorte |
| `reel` | 1 vídeo .mp4 | `cover` opcional (imagem de capa), `share_to_feed` padrão `true` |
| `story` | 1 imagem ou vídeo | sem legenda; figurinhas não são suportadas pela API |

`media` aceita caminho dentro do repo ou link público (https).

## O que a API não faz (fica no app do celular)

Fixar post, criar/editar destaque e capa, figurinha de link/enquete/pergunta, música do app, colaboradores em stories. Leva uns 5 minutos por semana.

## Configuração (uma vez)

1. **Token**: app "DANPE Publicador" em developers.facebook.com → caso de uso *Gerenciar mensagens e conteúdo no Instagram* → *Configuração da API com login do Instagram* → adicionar @somosdanpe → *Gerar token*.
2. **Secret**: Settings → Secrets and variables → Actions → `IG_TOKEN` = o token.
3. **Renovação automática (opcional, recomendado)**: crie um token do GitHub (fine-grained, só este repo, permissão *Secrets: Read and write*) e salve como secret `GH_PAT`. O fluxo *Renovar token do Instagram* roda nos dias 1 e 15.
4. **Repo privado?** Defina a variável `MEDIA_BASE_URL` com um endereço público que sirva este repo (ex.: um projeto do Cloudflare Pages ligado a ele). Em repo público não precisa.

## Rodar na mão

- Actions → *Publicar no Instagram* → *Run workflow*.
- Validar a fila localmente: `python publisher/publish.py --check`.
