  function updateSelectedPlaceMultiGame(buttonEl, place,whichBox) {
    const group = buttonEl.closest('.dropdown-group');
    group.querySelector('.event-dropdown').textContent = place;
    const sectionDropdown = group.querySelector('.section-dropdown');
    sectionDropdown.textContent = "Section";

    const sectionContainer = group.querySelector('.section-container');
    const sectionList = group.querySelector('.section-list');
    const goBtn = group.querySelector('.go-button');

    sectionList.innerHTML = "";

    const sections = placesData[place] || [];
    sections.forEach(section => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.className = "dropdown-item";
      btn.type = "button";
      btn.textContent = section;

      btn.onclick = function () {
        sectionDropdown.textContent = section;
        if (whichBox == 1){
            goBtn.href = `/graph?section=${encodeURIComponent(section)}&event=${encodeURIComponent(place)}`;
        }else{
            goBtn.href = `/predict?section=${encodeURIComponent(section)}&event=${encodeURIComponent(place)}`;
        }
        goBtn.hidden = false;
      };

      li.appendChild(btn);
      sectionList.appendChild(li);
    });

    sectionContainer.hidden = false;
    goBtn.hidden = true;
 }

   function updateSelectedPlaceSingleGame(buttonEl, place) {
    const group = buttonEl.closest('.dropdown-group');
    group.querySelector('.event-dropdown').textContent = place;
    const gameDropdown = group.querySelector('.individual-game-dropdown')
    gameDropdown.textContent = "Game";

    const gameContainer = group.querySelector('.individual-game-container');
    const gameList = group.querySelector('.individual-game-list');
    const sectionContainer = group.querySelector('.section-container');
    const sectionList = group.querySelector('.section-list');
    const goBtn = group.querySelector('.go-button');

    gameList.innerHTML = "";
    sectionList.innerHTML = "";
    sectionContainer.hidden = true;
    goBtn.hidden = true;

    const individualGame = gamesData[place] || [];
    for (let i = 0; i < individualGame.length; i++){
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.className = "dropdown-item";
      btn.type = "button";
      btn.textContent = individualGame[i];

      btn.onclick = function () {
        gameDropdown.textContent = individualGame[i];
        RevealSections(buttonEl,place,individualGame[i])
      };

      li.appendChild(btn);
      gameList.appendChild(li);
    }

    gameContainer.hidden = false;
 }
 function RevealSections(buttonEl,place,game){
    const group = buttonEl.closest('.dropdown-group');
    group.querySelector('.event-dropdown').textContent = place;
    const sectionDropdown = group.querySelector('.section-dropdown');
    sectionDropdown.textContent = "Section";

    const sectionContainer = group.querySelector('.section-container');
    const sectionList = group.querySelector('.section-list');
    const goBtn = group.querySelector('.go-button');

    sectionList.innerHTML = "";

    const gameSections = (gameSectionsData[place] && gameSectionsData[place][game]) || [];
    const sections = gameSections.length ? gameSections : (placesData[place] || []);
    sections.forEach(section => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.className = "dropdown-item";
      btn.type = "button";
      btn.textContent = section;

      btn.onclick = function () {
        sectionDropdown.textContent = section;
        goBtn.href = `/graph?section=${encodeURIComponent(section)}&event=${encodeURIComponent(place)}&game=${encodeURIComponent(game)}&mode=single`;
        goBtn.hidden = false;
      };

      li.appendChild(btn);
      sectionList.appendChild(li);
    });

    sectionContainer.hidden = false;
    goBtn.hidden = true;
 }
