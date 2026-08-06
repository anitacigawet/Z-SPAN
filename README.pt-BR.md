[RASCUNHO TEMPORÁRIO REDIGIDO POR IA. SERÁ REESCRITO ATÉ 4 DE AGOSTO DE 2026]

# Z-SPAN

[English](README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [**Português (Brasil)**](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Uma biblioteca virtual sobre política local.**

[Visite o Z-SPAN em zspan.org](https://zspan.org)

✨ **Publicada para consulta, preservação e inspiração.**

O Z-SPAN busca tornar as reuniões públicas locais mais fáceis de encontrar,
assistir e compreender. Lugares se tornam canais, reuniões se tornam episódios,
e os vídeos, pautas e atas originais continuam fazendo parte do caminho.

Este repositório é a biblioteca por trás da biblioteca: uma seleção de código-fonte
público, padrões de projeto e aprendizados que podem ser úteis para quem
esteja pensando em um projeto semelhante em outra cidade, estado ou país.

Ele não é uma cópia completa do sistema em produção e não foi criado para ser
clonado e lançado como outra instância do Z-SPAN. A unidade útil aqui é menor:
uma ideia de navegação, um limite claro para a reprodução, uma maneira de
manter as fontes visíveis ou um princípio de design que possa ser aproveitado
em um trabalho independente.

O [Respawn Kernel](respawn-kernel/README.md) é a exceção executável: um ponto
de partida independente para criar uma biblioteca de reuniões públicas para
qualquer país. O guia técnico completo está disponível atualmente em inglês.

> Esta página é uma tradução do README em inglês feita com auxílio de IA.
> Correções por pull request de pessoas fluentes em português são bem-vindas.
> Se houver diferença de sentido, prevalecem o [README em inglês](README.md),
> a [LICENSE](LICENSE) e o [NOTICE](NOTICE). Os demais documentos vinculados
> ainda estão em inglês.

---

## 📚 Por que esta biblioteca existe

Projetos que trabalham com documentos públicos locais costumam se deparar com
as mesmas perguntas:

- Como alguém pode navegar por reuniões quando cada site governamental as
  organiza de um jeito diferente?
- Como uma única interface pode continuar útil em diferentes cidades e com
  diferentes provedores de vídeo?
- Como manter claro o caminho de volta para uma fonte oficial?
- Como sistemas técnicos podem se explicar sem obrigar as pessoas a ler o
  banco de dados que existe por baixo deles?

O Z-SPAN é uma resposta que funciona, mas não é a única. O objetivo deste
repositório é deixar suas ideias úteis visíveis o bastante para que possam ser
examinadas, questionadas e levadas adiante por outros projetos.

## 👋 Para quem é esta biblioteca

Seja você estudante, ativista, jornalista, pesquisador, designer,
desenvolvedor, voluntário ou apenas alguém curioso sobre informações públicas
locais, não é preciso adotar o projeto inteiro para encontrar algo útil aqui.
A biblioteca é organizada para que uma ideia ou um componente possa ser
compreendido de cada vez.

## 🧭 Como usar este repositório

Não há uma ordem de leitura obrigatória, mas estes são bons pontos de partida:

1. Leia [o modelo do projeto](docs/PROJECT_MODEL.md) para encontrar a explicação
   mais simples de como as partes se relacionam.
2. Abra [o catálogo da biblioteca](CATALOG.md) para escolher uma seção de
   código, prompts ou design conforme a pergunta que você quer explorar.
3. Consulte [padrões que podem ser levados para outros projetos](docs/DESIGN_PATTERNS.md)
   para conhecer as ideias por trás da interface.
4. Use [o guia do repositório](docs/REPOSITORY_GUIDE.md) para acompanhar um
   percurso específico de um visitante pelo código publicado.
5. Confira [o que é e o que não é publicado](PUBLICATION_SCOPE.md) antes de
   tirar conclusões sobre o sistema mais amplo do Z-SPAN.
6. Veja [o registro do instantâneo atual](docs/snapshots/2026-08-02.md) para saber o
   tamanho exato e o estado de revisão desta publicação.

## 🗂️ O que está na coleção

O código publicado demonstra atualmente seis partes da experiência de uma
pessoa visitante:

- **Encontrar um lugar ou uma reunião** pela tela inicial e pelas telas de
  canal, cidade e busca.
- **Explorar o que está disponível** por meio de um guia que alterna entre
  cartões, mapa, reprodutor incorporado e uma visualização ampliada.
- **Voltar aos registros originais** por meio de links visíveis para vídeos,
  pautas e atas oficiais quando estiverem disponíveis.
- **Reproduzir vídeos em uma interface comum** mesmo quando a plataforma que
  hospeda o conteúdo muda.
- **Explicar verificações de integridade aos visitantes** por meio das telas de
  auditoria, escaneamento e verificação.
- **Transformar o registro de uma reunião em um resumo de fácil leitura sobre
  assuntos públicos** por meio de três exemplos de prompts revisados
  preservados na seção de prompts.

[UMA APRESENTAÇÃO VISUAL SERÁ ADICIONADA AQUI]

[O guia do repositório](docs/REPOSITORY_GUIDE.md) conecta cada uma dessas
ideias aos arquivos correspondentes.

## Uma observação sobre executar o código

Você não encontrará instruções de instalação, hospedagem, Docker ou
implantação neste repositório. Isso é intencional.

Os arquivos publicados foram selecionados de um sistema de trabalho privado
maior. Algumas importações, serviços, elementos de integração do aplicativo e
configurações de execução não estão incluídos. O código está aqui para ser lido e estudado;
ele não é apresentado como um aplicativo independente nem como uma
distribuição com suporte.

## Como o repositório está organizado

- [`docs/`](docs/) explica o modelo do projeto, padrões reutilizáveis, caminhos
  de leitura e instantâneos públicos datados.
- [`code/`](code/) contém uma seleção de código de referência para a interface
  de visitantes, organizada separadamente do caminho correspondente no projeto
  de trabalho privado.
- [`prompts/`](prompts/) contém três exemplos analisados e mantidos sem
  alterações que podem
  ser estudados ou adaptados individualmente.
- [`CATALOG.md`](CATALOG.md) é o índice seção por seção para pessoas e leitores
  de IA.
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) declara com clareza os limites
  da publicação.

A exportação pública altera apenas os nomes das seções. A estrutura relativa
dentro de `code/visitor-interface/src/` é preservada para que as relações entre
páginas, componentes, adaptadores de reprodução e estilos continuem legíveis.

## ⚖️ Licença

O código publicado está disponível sob a
[PolyForm Noncommercial License 1.0.0](LICENSE). Ele pode ser estudado,
adaptado, compartilhado e reutilizado para fins não comerciais de acordo com
os termos da licença. Isso inclui estudo pessoal, projetos de hobby, educação,
pesquisa pública, trabalho beneficente e uso governamental.

O uso comercial não é concedido por esta licença. A atribuição obrigatória e
os limites de uso do nome Z-SPAN estão registrados no [NOTICE](NOTICE).

## Contato

O projeto está hospedado em [zspan.org](https://zspan.org). Se você tiver
interesse em ocupar uma vaga disponível no ecossistema Z-SPAN, entre em contato
pelo e-mail [anitacigawet@pm.me](mailto:anitacigawet@pm.me) para obter mais
informações.
