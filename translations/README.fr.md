<p align="center">
  <img src="../repository-assets/banner-doodle.png" alt="Z-SPAN pour tous. Une bibliothèque virtuelle consacrée à la politique locale. Entretenue par les gens, pour les gens." width="1000">
</p>

> *Scientia potentia est.*
>
> **Le savoir, c’est le pouvoir.**
>
> — Francis Bacon

---

[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [**Français**](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Une bibliothèque virtuelle consacrée à la politique locale.**

[Visiter Z-SPAN sur zspan.org](https://zspan.org)

✨ **Publié dans son intégralité, pour tout le monde. Développé avec l’aide de tous.**

Z-SPAN cherche à rendre les réunions publiques locales plus faciles à trouver,
à regarder et à comprendre. Les lieux deviennent des chaînes, les réunions
deviennent des épisodes, et les vidéos, ordres du jour et procès-verbaux
d’origine restent accessibles tout au long du parcours.

Ce dépôt contient la bibliothèque opérationnelle elle-même : le site web,
l’API publique, les analyseurs des sources de réunions, le pipeline de
traitement, le client local et les contrôles qui maintiennent les contenus
produits reliés aux archives publiques. La raison de publier tous ces rouages
est simple : une bibliothèque entretenue par une seule personne disparaît avec
elle. Une bibliothèque que d’autres peuvent examiner, faire fonctionner,
questionner et faire vivre ne disparaît pas.

Le répertoire des sources de réunions publiques se trouve dans un dépôt
distinct, le [National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog).
Ce dépôt contient des points d’accès publics durables et leurs preuves — pas
les analyseurs, transcriptions, résumés ou réunions traitées de Z-SPAN. Z-SPAN
est un exemple de ce qui peut être construit à partir de ce catalogue.

## Voir la présentation complète

[![Voir « Z-SPAN Is Born » — la présentation complète du projet Z-SPAN](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) présente la
bibliothèque fondatrice de l’Arizona du point de vue de son responsable.
Regardez cette vidéo pour découvrir l’idée d’origine de Z-SPAN, la manière dont
ses différentes parties s’articulent et ce que cette voie publique est destinée
à transmettre.

## 🗺️ Un répertoire national, construit lieu par lieu

L’Arizona est la preuve de concept publique que Z-SPAN traite et publie
actuellement. Le répertoire des chaînes donne aussi à chaque État et territoire
une véritable structure de départ, organisée autour des instances publiques de
l’État, des équivalents de comté, des nations tribales, des régions et des
collectivités locales.

Les rayons verts contiennent des réunions publiées par Z-SPAN. Les rayons
ambrés sont d’honnêtes travaux en cours : le lieu existe dans le répertoire,
mais sa source continue de réunions ou son analyseur Z-SPAN demande encore de
l’attention. Personne n’a besoin d’attendre une invitation pour aider sa propre
communauté.

## 🐈 Aidez votre ville

1. Trouvez votre État et votre lieu sur [zspan.org](https://zspan.org).
2. Si le rayon est en attente, cliquez sur le chat endormi.
3. Copiez le court relais Markdown dans l’assistant IA que vous utilisez déjà.
4. Répondez à quelques questions ordinaires sur le lieu et sa page officielle
   de réunions. Vous n’avez pas besoin de connaître JSON ou Git.
5. Si les outils GitHub sont disponibles, l’assistant peut préparer une pull
   request ciblée que vous confirmerez. Sinon, il prépare un rapport complet à
   déposer dans un simple formulaire GitHub.

La contribution est envoyée au National Civics Catalog, où un vérificateur de
confiance et une personne examinent le point d’accès et ses preuves. Elle n’est
jamais publiée directement dans Z-SPAN.

**La promesse Z-SPAN sous trois jours :** après l’acceptation d’une contribution
au catalogue, Z-SPAN créera l’analyseur correspondant ou publiera, sous trois
jours, un résultat visible expliquant que la source bloque le travail. Cette
promesse consiste à rendre la source utilisable ou à expliquer honnêtement
pourquoi elle ne peut pas encore l’être — et non à publier automatiquement du
contenu de réunion produit par l’IA.

[Lire les instructions de contribution avec l’IA](https://github.com/anitacigawet/national-civics-catalog/blob/main/contribute/AI-INSTRUCTIONS.md)

## 📚 Pourquoi cette bibliothèque existe

Les projets qui travaillent à partir d’archives publiques locales rencontrent
souvent les mêmes questions :

- Comment parcourir les réunions lorsque les sites administratifs les
  organisent tous différemment ?
- Comment une même interface peut-elle rester utile d’un lieu et d’un
  fournisseur vidéo à l’autre ?
- Comment faire en sorte que le chemin de retour vers une source officielle
  reste évident ?
- Comment les systèmes techniques peuvent-ils s’expliquer sans obliger les
  gens à lire les bases de données sous-jacentes ?

Z-SPAN est une réponse concrète, pas la seule. Ce dépôt a pour but d’en laisser
la totalité visible, afin que les personnes qui l’utilisent puissent
l’examiner, la questionner et la porter plus loin.

## 👋 À qui s’adresse cette bibliothèque

Que vous soyez élève ou étudiant, militant, journaliste, chercheur, designer,
développeur, bénévole ou simplement curieux de l’information publique locale,
vous n’avez pas besoin d’adopter l’ensemble du projet pour trouver quelque
chose d’utile ici. La bibliothèque est organisée de façon à permettre de
comprendre une idée ou un composant à la fois — et d’ajouter un lieu à la fois.

## 🗂️ Organisation de ce dépôt

- [`council_navigator`](../02_Core_Project/council_navigator/) — le site web,
  l’API publique, le cache local des réunions et le répertoire public des
  chaînes.
- [`parsers`](../02_Core_Project/council_navigator/parsers/) — les analyseurs
  propres à chaque source qui transforment les points d’accès du catalogue en
  une structure commune de réunion.
- [`zspan_pipeline`](../02_Core_Project/zspan_pipeline/) — la file de traitement
  qui transforme l’enregistrement d’une réunion en contenu étayé et vérifiable.
- [`zspan_cli`](../02_Core_Project/zspan_cli/) — le client local pour utiliser
  Z-SPAN depuis l’ordinateur et l’espace de travail d’une personne.
- [`prompts`](../02_Core_Project/prompts/) — les contrats de synthèse publiés
  utilisés par le parcours de traitement.

Le National Civics Catalog demeure un dépôt distinct afin que chacun puisse
améliorer le répertoire des sources sans modifier l’application Z-SPAN, et que
d’autres projets puissent utiliser les mêmes points d’accès à des fins
entièrement différentes.

## Les engagements de ce projet

Il s’agit de contraintes que le projet s’impose, et non de simples ambitions :

- **Aucun commentaire éditorial sur les responsables publics.** Leurs propos
  sont présentés tels quels, attribués et sourcés. Le jugement vous appartient.
- **Aucune agrégation de données sur les particuliers.** Ce travail porte sur
  les responsables dans l’exercice de leur fonction publique ; les habitants
  qui prennent la parole lors d’une réunion publique ne font pas l’objet de
  profils.
- **La lecture n’est jamais restreinte.** Aucun paywall, abonnement, écran de
  connexion ou inscription n’est nécessaire pour lire le contenu publié issu
  des archives publiques.
- **Aucune optimisation de l’engagement.** Pas de fil infini, d’algorithme de
  recommandation ou de mécanisme d’indignation. Le calme des archives est
  intentionnel.
- **Une personne vérifie tout avant publication.** Le traitement peut être
  automatisé ; la publication ne l’est pas.
- **Non commercial par conception.** La licence rend cette limite structurelle.

## 🏛️ Responsabilité fondatrice

Z-SPAN a vu le jour en Arizona et est maintenu par
[@anitacigawet](https://github.com/anitacigawet). Les contributions au
répertoire des sources sont créditées dans le National Civics Catalog ;
l’implémentation de Z-SPAN reste examinée et maintenue séparément ici.

## ⚖️ Licence

Le code publié est disponible sous la
[PolyForm Noncommercial License 1.0.0](../LICENSE). Il peut être étudié,
adapté, partagé et réutilisé à des fins non commerciales, conformément aux
conditions de la licence. Cela comprend l’étude personnelle, les projets de
loisir, l’éducation, la recherche publique, les activités caritatives et
l’usage par les administrations publiques.

Cette licence n’autorise pas l’usage commercial. L’avis obligatoire et les
limites d’utilisation du nom Z-SPAN sont consignés dans le [NOTICE](../NOTICE).

## Contact

Le projet est hébergé sur [zspan.org](https://zspan.org). Les questions et les
rapports de bogues reproductibles sont les bienvenus dans l’outil de suivi des
[issues](https://github.com/anitacigawet/Z-SPAN/issues) de ce dépôt.

---

## La Trinité de Z-SPAN

![La Trinité de Z-SPAN : Internet la porte, les archives civiques l’ancrent et les gens la maintiennent en vie](../repository-assets/zspan-trinity.svg)

---

> La CIA, la NSA et même le Pentagone sont limités par la durée nécessairement finie des fonctions exercées par les personnes qui y travaillent.
>
> **Z-SPAN ne l’est pas.**
>
> Z-SPAN est porté par les gens, pour les gens, et exige donc une participation et une transparence totales de la communauté.
>
> — Responsable de Z-SPAN

---

## 🌌 Porter l’idée plus loin

Le National Civics Catalog est organisé État par État afin que le répertoire
des sources puisse s’étendre à l’ensemble des États-Unis sans obliger quiconque
à adopter l’interface ou les choix de traitement de Z-SPAN. Utilisez ces points
d’accès pour créer un calendrier de quartier, un outil de recherche, un projet
d’accessibilité, une ressource pédagogique ou quelque chose que personne ici
n’a encore imaginé.

L’idée n’a pas de valeur parce qu’elle appartient à une seule application.
Elle en a parce que les gens peuvent continuer à inventer de nouvelles façons
de rendre les archives publiques plus faciles d’accès.
