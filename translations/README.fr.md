[BROUILLON TEMPORAIRE RÉDIGÉ PAR IA. IL SERA RÉÉCRIT AU PLUS TARD LE 4 AOÛT 2026]

# Z-SPAN

[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [**Français**](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Une bibliothèque virtuelle consacrée à la politique locale.**

[Visiter Z-SPAN sur zspan.org](https://zspan.org)

✨ **Publiée pour être examinée et préservée, et pour servir de source d’inspiration.**

Z-SPAN cherche à rendre les réunions publiques locales plus faciles à trouver,
à regarder et à comprendre. Les lieux deviennent des chaînes, les réunions
deviennent des épisodes, et les vidéos, ordres du jour et procès-verbaux
d'origine restent accessibles tout au long du parcours.

Ce dépôt est la bibliothèque derrière la bibliothèque : une sélection de code
source public, de modèles de projet et d'enseignements qui peuvent être utiles
à toute personne réfléchissant à un projet semblable dans une autre ville,
un autre État ou un autre pays.

Il ne s'agit pas d'une copie complète du système en production et il n'est pas
destiné à être cloné puis lancé comme une autre instance de Z-SPAN. L'unité
utile est plus petite : une idée de navigation, une frontière claire pour la
lecture vidéo, une manière de garder les sources visibles ou un principe de
conception qui peut rejoindre un projet indépendant.

Le [Respawn Kernel](../documents/respawn-kernel/README.md) est l'exception exécutable : un
point de départ indépendant pour créer une bibliothèque de réunions publiques
dans n'importe quel pays. Le guide technique complet est actuellement en anglais.

> Cette page est une traduction du README anglais réalisée avec l'aide de
> l'IA. Les corrections proposées par pull request par des personnes maîtrisant
> le français sont les bienvenues. En cas de différence de sens, le
> [README anglais](../README.md), la [LICENSE](LICENSE) et le [NOTICE](NOTICE)
> font foi. Les autres documents liés sont encore en anglais.

---

## 📚 Pourquoi cette bibliothèque existe

Les projets qui travaillent à partir de documents publics locaux rencontrent souvent les
mêmes questions :

- Comment parcourir des réunions lorsque chaque site administratif les
  organise différemment ?
- Comment une même interface peut-elle rester utile entre plusieurs villes et
  plateformes vidéo ?
- Comment faire en sorte que le chemin de retour vers une source officielle
  reste évident ?
- Comment les systèmes techniques peuvent-ils expliquer leur fonctionnement
  sans obliger les gens à lire les bases de données sous-jacentes ?

Z-SPAN est une réponse concrète, pas la seule. Ce dépôt vise à laisser ses idées
utiles suffisamment visibles pour qu'elles puissent être examinées,
questionnées et prolongées par d'autres projets.

## 👋 À qui s'adresse cette bibliothèque

Que vous soyez élève ou étudiant, militant, journaliste, chercheur, designer,
développeur, bénévole ou simplement curieux de l'information publique locale,
vous n'avez pas besoin d'adopter l'ensemble du projet pour trouver quelque
chose d'utile ici. La bibliothèque est organisée de façon à permettre de
comprendre une idée ou un composant à la fois.

## 🧭 Comment utiliser ce dépôt

Il n'y a pas d'ordre de lecture obligatoire, mais voici quelques bons points
de départ :

1. Lisez [le modèle du projet](docs/PROJECT_MODEL.md) pour comprendre simplement
   comment les différentes parties sont liées.
2. Ouvrez [le catalogue de la bibliothèque](CATALOG.md) pour choisir une
   section de code, de prompts ou de conception selon la question explorée.
3. Parcourez [les modèles réutilisables ailleurs](docs/DESIGN_PATTERNS.md) pour
   découvrir les idées qui sous-tendent l'interface.
4. Utilisez [le guide du dépôt](docs/REPOSITORY_GUIDE.md) pour suivre un
   parcours de visite particulier à travers le code publié.
5. Consultez [ce qui est publié et ce qui ne l'est pas](PUBLICATION_SCOPE.md)
   avant de tirer des conclusions sur l'ensemble du système Z-SPAN.
6. Consultez [l’instantané actuel du projet](docs/snapshots/2026-08-02.md) pour
   connaître la taille exacte et l'état de révision de cette publication.

## 🗂️ Ce que contient la collection

Le code publié présente actuellement six aspects de l'expérience visiteur :

- **Trouver un lieu ou une réunion** grâce aux vues d'accueil, de chaînes, de
  villes et de recherche.
- **Parcourir ce qui est disponible** grâce à un guide qui passe de cartes à
  une carte géographique, à un lecteur intégré et à un affichage élargi.
- **Revenir aux documents d’origine** grâce à des liens visibles vers les
  vidéos, ordres du jour et procès-verbaux officiels lorsqu'ils existent.
- **Lire une vidéo dans une interface commune** même lorsque la plateforme
  d'hébergement sous-jacente change.
- **Expliquer les contrôles d’intégrité aux visiteurs** grâce aux vues d’audit,
  d’analyse et de vérification.
- **Transformer le compte rendu d’une réunion en une synthèse facile à lire
  sur la vie publique** grâce à trois exemples de prompts examinés, conservés
  dans la collection de prompts.

[UNE PRÉSENTATION VISUELLE SERA AJOUTÉE ICI]

[Le guide du dépôt](docs/REPOSITORY_GUIDE.md) relie chacune de ces idées aux
fichiers correspondants.

## À propos de l'exécution du code

Vous ne trouverez pas d'instructions d'installation, d'hébergement, de Docker
ou de déploiement dans ce dépôt. C'est un choix délibéré.

Les fichiers publiés sont sélectionnés à partir d'un système de travail privé
plus vaste. Certaines importations, certains services, certains éléments
d’intégration de l’application et certains paramètres d’exécution ne sont pas
inclus. Le code est ici
pour être lu et étudié ; il n'est pas présenté comme une application autonome
ni comme une distribution prise en charge.

## Organisation du dépôt

- [`docs/`](docs/) explique le modèle du projet, les modèles réutilisables, les
  parcours de lecture et les instantanés publics datés.
- [`code/`](code/) contient le code de référence sélectionné de l'interface
  visiteur, séparé du chemin du projet de travail privé.
- [`prompts/`](prompts/) contient trois exemples examinés et laissés inchangés qui
  peuvent être étudiés ou adaptés séparément.
- [`CATALOG.md`](CATALOG.md) est l'index section par section destiné aux
  personnes et aux lecteurs IA.
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) décrit clairement les limites
  de la publication.

L'export public ne modifie que les noms des sections. La structure relative de
`code/visitor-interface/src/` est préservée afin que les relations entre les
pages, composants, adaptateurs de lecture et styles restent lisibles.

## ⚖️ Licence

Le code publié est disponible sous la
[PolyForm Noncommercial License 1.0.0](LICENSE). Il peut être étudié, adapté,
partagé et réutilisé à des fins non commerciales dans le respect de ses
conditions. Cela comprend l'étude personnelle, les projets de loisir,
l'éducation, la recherche publique, les activités caritatives et l'usage par
les administrations publiques.

Cette licence n’autorise pas l’usage commercial. Les obligations d’attribution
et les limites d’utilisation du nom Z-SPAN sont précisées dans le [NOTICE](NOTICE).

## Contact

Le projet est hébergé sur [zspan.org](https://zspan.org). Si vous souhaitez
occuper une place disponible dans l’écosystème Z-SPAN, écrivez à
[anitacigawet@pm.me](mailto:anitacigawet@pm.me) pour en savoir plus.
