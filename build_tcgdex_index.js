#!/usr/bin/env node
/* Construit un index compact depuis la base officielle TCGdex (licence MIT). */

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");

function argumentsLigneCommande(argv) {
  const resultat = {};
  for (let i = 2; i < argv.length; i += 2) {
    const cle = argv[i];
    const valeur = argv[i + 1];
    if (!cle?.startsWith("--") || !valeur) {
      throw new Error("Usage: node build_tcgdex_index.js --source DOSSIER --output FICHIER");
    }
    resultat[cle.slice(2)] = valeur;
  }
  if (!resultat.source || !resultat.output) {
    throw new Error("Les options --source et --output sont obligatoires.");
  }
  return resultat;
}

function extraireObjet(source, variable) {
  const motif = new RegExp(`const\\s+${variable}\\s*:\\s*\\w+\\s*=`);
  const correspondance = motif.exec(source);
  if (!correspondance) throw new Error(`Objet ${variable} introuvable`);
  const debut = source.indexOf("{", correspondance.index + correspondance[0].length);
  if (debut < 0) throw new Error(`Début de ${variable} introuvable`);

  let profondeur = 0;
  let chaine = null;
  let echappe = false;
  let commentaireLigne = false;
  let commentaireBloc = false;
  for (let i = debut; i < source.length; i += 1) {
    const caractere = source[i];
    const suivant = source[i + 1];
    if (commentaireLigne) {
      if (caractere === "\n") commentaireLigne = false;
      continue;
    }
    if (commentaireBloc) {
      if (caractere === "*" && suivant === "/") {
        commentaireBloc = false;
        i += 1;
      }
      continue;
    }
    if (chaine) {
      if (echappe) echappe = false;
      else if (caractere === "\\") echappe = true;
      else if (caractere === chaine) chaine = null;
      continue;
    }
    if (caractere === "/" && suivant === "/") {
      commentaireLigne = true;
      i += 1;
      continue;
    }
    if (caractere === "/" && suivant === "*") {
      commentaireBloc = true;
      i += 1;
      continue;
    }
    if (caractere === '"' || caractere === "'" || caractere === "`") {
      chaine = caractere;
      continue;
    }
    if (caractere === "{") profondeur += 1;
    if (caractere === "}") {
      profondeur -= 1;
      if (profondeur === 0) return source.slice(debut, i + 1);
    }
  }
  throw new Error(`Fin de ${variable} introuvable`);
}

function lireObjet(fichier, variable, contexte = {}) {
  const source = fs.readFileSync(fichier, "utf8");
  const litteral = extraireObjet(source, variable);
  return vm.runInNewContext(`(${litteral})`, contexte, {
    timeout: 100,
    contextCodeGeneration: { strings: false, wasm: false },
  });
}

function langues(objet) {
  if (!objet || typeof objet !== "object") return {};
  return { fr: objet.fr || null, en: objet.en || null };
}

function nomsElements(elements) {
  const resultat = { fr: [], en: [] };
  for (const element of elements || []) {
    const nom = langues(element?.name);
    if (nom.fr) resultat.fr.push(nom.fr);
    if (nom.en) resultat.en.push(nom.en);
  }
  return resultat;
}

function nombreCardmarket(objet) {
  const valeur = objet?.thirdParty?.cardmarket;
  return Number.isInteger(valeur) ? valeur : null;
}

function variantesCompactes(variantes) {
  if (!Array.isArray(variantes)) return [];
  return variantes.map((variante) => ({
    type: variante.type || null,
    subtype: variante.subtype || null,
    size: variante.size || null,
    stamp: Array.isArray(variante.stamp) ? variante.stamp : [],
    foil: variante.foil || null,
    cm: nombreCardmarket(variante),
  }));
}

function fichiersSets(dossierData) {
  const resultat = [];
  for (const serie of fs.readdirSync(dossierData, { withFileTypes: true })) {
    if (!serie.isDirectory()) continue;
    const dossierSerie = path.join(dossierData, serie.name);
    for (const entree of fs.readdirSync(dossierSerie, { withFileTypes: true })) {
      if (entree.isFile() && entree.name.endsWith(".ts")) {
        resultat.push({ serie: serie.name, fichier: path.join(dossierSerie, entree.name) });
      }
    }
  }
  return resultat;
}

function main() {
  const options = argumentsLigneCommande(process.argv);
  const source = path.resolve(options.source);
  const dossierData = path.join(source, "data");
  const index = { version: 1, source_commit: null, sets: {} };
  try {
    index.source_commit = execFileSync("git", ["-C", source, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();
  } catch (_) {
    index.source_commit = "inconnu";
  }

  let cartesAjoutees = 0;
  for (const entreeSet of fichiersSets(dossierData)) {
    let set;
    try {
      set = lireObjet(entreeSet.fichier, "set", { serie: {} });
    } catch (_) {
      continue;
    }
    const total = Number(set?.cardCount?.official);
    if (!Number.isInteger(total) || !set?.name?.fr || !set?.id) continue;

    const nomFichier = path.basename(entreeSet.fichier, ".ts");
    const dossierCartes = path.join(dossierData, entreeSet.serie, nomFichier);
    if (!fs.existsSync(dossierCartes)) continue;
    const cartes = {};
    for (const entreeCarte of fs.readdirSync(dossierCartes, { withFileTypes: true })) {
      if (!entreeCarte.isFile() || !entreeCarte.name.endsWith(".ts")) continue;
      const localId = path.basename(entreeCarte.name, ".ts");
      let carte;
      try {
        carte = lireObjet(path.join(dossierCartes, entreeCarte.name), "card", { Set: set });
      } catch (_) {
        continue;
      }
      if (!carte?.name?.fr) continue;
      const cm = nombreCardmarket(carte);
      const variantes = variantesCompactes(carte.variants);
      if (!cm && !variantes.some((variante) => variante.cm)) continue;
      cartes[localId] = {
        name: langues(carte.name),
        attacks: nomsElements(carte.attacks),
        abilities: nomsElements(carte.abilities),
        variants: variantes,
        cm,
      };
      cartesAjoutees += 1;
    }
    if (!Object.keys(cartes).length) continue;
    const resumeSet = {
      id: set.id,
      name: langues(set.name),
      cards: cartes,
    };
    (index.sets[String(total)] ||= []).push(resumeSet);
  }

  fs.mkdirSync(path.dirname(path.resolve(options.output)), { recursive: true });
  fs.writeFileSync(options.output, JSON.stringify(index));
  console.log(`Index TCGdex créé : ${cartesAjoutees} cartes françaises.`);
}

main();
