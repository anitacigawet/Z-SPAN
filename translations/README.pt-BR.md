<p align="center">
  <img src="../repository-assets/banner-doodle.png" alt="Z-SPAN para todos. Uma biblioteca virtual sobre política local. Mantida pelas pessoas, para as pessoas." width="1000">
</p>

> *Scientia potentia est.*
>
> **Conhecimento é poder.**
>
> — Francis Bacon

---

[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [**Português (Brasil)**](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Uma biblioteca virtual sobre política local.**

[Visite o Z-SPAN em zspan.org](https://zspan.org)

✨ **Publicado por inteiro, para qualquer pessoa. Ampliado com a ajuda de qualquer pessoa.**

O Z-SPAN busca tornar as reuniões públicas locais mais fáceis de encontrar,
assistir e compreender. Lugares se tornam canais, reuniões se tornam episódios,
e os vídeos, pautas e atas originais continuam fazendo parte do caminho.

Este repositório contém a própria biblioteca em funcionamento: o site, a API
pública, os parsers de fontes de reuniões, o fluxo de processamento, o cliente
local e as verificações que mantêm o trabalho gerado ligado ao registro público.
O motivo para publicar todo esse mecanismo é simples: uma biblioteca mantida
por uma pessoa termina com essa pessoa. Uma biblioteca que outras pessoas
podem examinar, executar, questionar e levar adiante não termina.

O diretório de fontes de reuniões governamentais fica separado no
[National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog).
Esse repositório contém endpoints públicos contínuos e suas evidências — não os
parsers, transcrições, resumos ou reuniões processadas do Z-SPAN. O Z-SPAN é um
exemplo do que pode ser construído a partir dele.

## Assista à apresentação completa

[![Assista a “Z-SPAN Is Born” — a apresentação completa do projeto Z-SPAN](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) apresenta a
biblioteca fundadora do Arizona pela perspectiva de quem a mantém. Assista para
conhecer a visão original do Z-SPAN, como suas partes se encaixam e o que esse
caminho público pretende levar adiante.

## 🗺️ Um diretório nacional, construído lugar por lugar

O Arizona é a prova de conceito pública que o Z-SPAN atualmente processa e
publica. O diretório de canais também oferece a cada estado e território uma
estrutura inicial real, organizada em torno de órgãos públicos estaduais,
equivalentes a condados, Tribais, regionais e locais.

As estantes verdes têm reuniões publicadas pelo Z-SPAN. As estantes âmbar são
trabalhos em andamento apresentados com honestidade: o lugar existe no
diretório, mas sua fonte contínua de reuniões ou seu parser do Z-SPAN ainda
precisa de atenção. Ninguém precisa esperar um convite para ajudar a própria
comunidade.

## 🐈 Ajude sua cidade

1. Encontre seu estado e seu lugar em [zspan.org](https://zspan.org).
2. Se a estante estiver esperando, clique no gato adormecido.
3. Copie o breve repasse em Markdown para o assistente de IA que você já usa.
4. Responda a algumas perguntas comuns sobre o lugar e sua página oficial de
   reuniões. Você não precisa conhecer JSON nem Git.
5. Se as ferramentas do GitHub estiverem disponíveis, o assistente poderá
   preparar um pull request focado para sua confirmação. Caso contrário, ele
   prepara um relatório completo para um formulário simples do GitHub.

A contribuição vai para o National Civics Catalog, onde um verificador de
confiança e uma pessoa analisam o endpoint e suas evidências. Ela nunca é
publicada diretamente no Z-SPAN.

**A promessa de três dias do Z-SPAN:** depois que uma contribuição ao catálogo
for aceita, o Z-SPAN criará o parser correspondente ou publicará, em até três
dias, um resultado visível informando que a fonte impede o trabalho. A promessa
é tornar a fonte utilizável ou explicar com honestidade por que ela ainda não
pode ser usada — e não publicar automaticamente conteúdo de reuniões gerado
por IA.

[Leia as instruções de contribuição com IA](https://github.com/anitacigawet/national-civics-catalog/blob/main/contribute/AI-INSTRUCTIONS.md)

## 📚 Por que esta biblioteca existe

Projetos que trabalham com registros públicos locais costumam se deparar com
as mesmas perguntas:

- Como alguém pode navegar por reuniões quando os sites governamentais as
  organizam de formas diferentes?
- Como uma única interface pode continuar útil em diferentes lugares e com
  diferentes provedores de vídeo?
- Como manter claro o caminho de volta para uma fonte oficial?
- Como sistemas técnicos podem se explicar sem obrigar as pessoas a ler o
  banco de dados que existe por baixo deles?

O Z-SPAN é uma resposta que funciona, mas não é a única. O objetivo deste
repositório é deixar tudo visível — para ser examinado, questionado e levado
adiante pelas pessoas que o utilizam.

## 👋 Para quem é esta biblioteca

Seja você estudante, ativista, jornalista, pesquisador, designer,
desenvolvedor, voluntário ou apenas alguém curioso sobre informações públicas
locais, não é preciso adotar o projeto inteiro para encontrar algo útil aqui.
A biblioteca é organizada para que uma ideia ou um componente possa ser
compreendido de cada vez — e para que um lugar possa ser adicionado de cada vez.

## 🗂️ Como este repositório está organizado

- [`council_navigator`](../02_Core_Project/council_navigator/) — o site, a API
  pública, o cache local de reuniões e o diretório público de canais.
- [`parsers`](../02_Core_Project/council_navigator/parsers/) — os parsers de
  calendário específicos de cada fonte que transformam endpoints do catálogo
  em um formato comum de reunião.
- [`zspan_pipeline`](../02_Core_Project/zspan_pipeline/) — a fila de
  processamento que transforma a gravação de uma reunião em material baseado
  em fontes e pronto para revisão.
- [`zspan_cli`](../02_Core_Project/zspan_cli/) — o cliente local para usar o
  Z-SPAN no computador e no espaço de trabalho da própria pessoa.
- [`prompts`](../02_Core_Project/prompts/) — os contratos de síntese publicados
  que são usados no fluxo de processamento.

O National Civics Catalog permanece em um repositório separado para que as
pessoas possam melhorar o diretório de fontes sem alterar o aplicativo Z-SPAN,
e para que outros projetos possam usar os mesmos endpoints para finalidades
completamente diferentes.

## Os compromissos deste projeto

Estas são limitações que o projeto impõe a si mesmo, não apenas aspirações:

- **Nenhuma opinião editorial sobre autoridades públicas.** Suas palavras são
  apresentadas literalmente, com atribuição e fonte. O julgamento é seu.
- **Nenhuma agregação de dados sobre cidadãos particulares.** O trabalho trata
  de autoridades no exercício de suas funções públicas; moradores que falam em
  um microfone público não são perfilados.
- **A leitura nunca é bloqueada.** Não é necessário paywall, assinatura, tela
  de login ou cadastro para ler o conteúdo publicado de registros públicos.
- **Nenhuma otimização de engajamento.** Não há feeds infinitos, algoritmos de
  recomendação nem mecanismos de indignação. O registro é calmo de propósito.
- **Uma pessoa revisa tudo antes da publicação.** O processamento pode ser
  automatizado; a publicação não.
- **Não comercial desde a concepção.** A licença torna esse limite estrutural.

## 🏛️ Cuidado desde a fundação

O Z-SPAN começou no Arizona e é mantido por
[@anitacigawet](https://github.com/anitacigawet). As contribuições para o
diretório de fontes recebem crédito no National Civics Catalog; a implementação
do Z-SPAN continua sendo revisada e mantida separadamente aqui.

## ⚖️ Licença

O código publicado está disponível sob a
[PolyForm Noncommercial License 1.0.0](../LICENSE). Ele pode ser estudado,
adaptado, compartilhado e reutilizado para fins não comerciais, conforme os
termos da licença. Isso inclui estudo pessoal, projetos de hobby, educação,
pesquisa pública, trabalho beneficente e uso governamental.

O uso comercial não é concedido por esta licença. O aviso obrigatório e os
limites de uso do nome Z-SPAN estão registrados no [NOTICE](../NOTICE).

## Contato

O projeto está hospedado em [zspan.org](https://zspan.org). Perguntas e relatos
de bugs reproduzíveis são bem-vindos no
[rastreador de issues](https://github.com/anitacigawet/Z-SPAN/issues) deste
repositório.

---

## A Trindade do Z-SPAN

![A Trindade do Z-SPAN: a internet o transporta, os registros cívicos lhe dão base e as pessoas o mantêm vivo](../repository-assets/zspan-trinity.svg)

---

> A CIA, a NSA e até mesmo o Pentágono são limitados pelo tempo finito de permanência das pessoas que trabalham neles.
>
> **O Z-SPAN não.**
>
> O Z-SPAN é movido pelas pessoas, para as pessoas, e por isso exige o envolvimento e a transparência de toda a comunidade.
>
> — Responsável pelo Z-SPAN

---

## 🌌 Leve a ideia mais longe

O National Civics Catalog é organizado estado por estado para que o diretório
de fontes possa crescer por todos os Estados Unidos sem exigir que ninguém
adote as escolhas de interface ou processamento do Z-SPAN. Use os endpoints
para criar um calendário de bairro, uma ferramenta de pesquisa, um projeto de
acessibilidade, um recurso para sala de aula ou algo que ninguém aqui ainda
imaginou.

A ideia não é valiosa porque pertence a um único aplicativo. Ela é valiosa
porque as pessoas podem continuar encontrando novas maneiras de tornar o
registro público mais fácil de acessar.
