/* Busca de cidades no navegador: carrega só o arquivo do índice que interessa (busca/c-<letra>.json). */
(function () {
  var campo = document.getElementById("q"), lista = document.getElementById("res");
  if (!campo || !lista) return;
  var rel = campo.getAttribute("data-rel") || "";
  var cache = {}, pend = 0;
  function norm(s) { return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").trim(); }
  function carregar(letra) {
    if (cache[letra]) return Promise.resolve(cache[letra]);
    return fetch(rel + "busca/c-" + letra + ".json").then(function (r) { return r.ok ? r.json() : []; }).then(function (d) { cache[letra] = d; return d; }).catch(function () { return []; });
  }
  function mostrar(itens, q) {
    lista.innerHTML = "";
    if (!itens.length) { lista.hidden = q.length < 2; if (q.length >= 2) lista.innerHTML = '<li><a href="#" tabindex="-1">Nenhuma cidade com estudo para “' + q.replace(/</g, "") + '” ainda.</a></li>'; return; }
    itens.slice(0, 8).forEach(function (it) {
      var li = document.createElement("li"), a = document.createElement("a");
      a.href = rel + it[2] + "index.html";
      a.innerHTML = "<span></span><small></small>";
      a.firstChild.textContent = it[0]; a.lastChild.textContent = it[1];
      li.appendChild(a); lista.appendChild(li);
    });
    lista.hidden = false;
  }
  campo.addEventListener("input", function () {
    var q = norm(campo.value), id = ++pend;
    if (q.length < 2) { lista.hidden = true; return; }
    carregar(q.charAt(0)).then(function (dados) {
      if (id !== pend) return;
      var achados = dados.filter(function (it) { return norm(it[0]).indexOf(q) !== -1; });
      achados.sort(function (a, b) { var pa = norm(a[0]).indexOf(q) === 0 ? 0 : 1, pb = norm(b[0]).indexOf(q) === 0 ? 0 : 1; return pa - pb || b[3] - a[3]; });
      mostrar(achados, q);
    });
  });
  document.addEventListener("click", function (e) { if (!lista.contains(e.target) && e.target !== campo) lista.hidden = true; });
  campo.addEventListener("keydown", function (e) { if (e.key === "Escape") lista.hidden = true; });
})();
