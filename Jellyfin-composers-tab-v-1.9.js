function jfComposerMain() {
    'use strict';

    var version = '1.9.0';
    var prefix = '[Composers Tab ' + version + ']';
    var tabId = 'jf-composers-v19-tab';
    var panelId = 'jf-composers-v19-panel';
    var styleId = 'jf-composers-v19-style';
    var scanTimer = null;
    var lastTabLabels = '';
    var loading = false;
    var active = false;
    var pageSize = 100;
    var currentPage = 0;
    var allComposers = [];
    var composerLibraryId = null;
    var fullListLoaded = false;
    var pendingPage = 0;
    var backgroundLoad = false;
    var prefetchTimer = null;
    var historyStateKey = 'jfComposersViewV19';

    function log(message, value) {
        if (arguments.length > 1) {
            console.info(prefix, message, value);
        } else {
            console.info(prefix, message);
        }
    }

    function cleanText(element) {
        var value = '';

        if (element && element.textContent) {
            value = element.textContent;
        }

        return value.replace(/\s+/g, ' ').trim();
    }

    function isMusicLabel(label) {
        if (label === 'Albums') {
            return true;
        }

        if (label === 'Suggestions') {
            return true;
        }

        if (label === 'Album artists') {
            return true;
        }

        if (label === 'Artists') {
            return true;
        }

        if (label === 'Playlists') {
            return true;
        }

        if (label === 'Songs') {
            return true;
        }

        if (label === 'Genres') {
            return true;
        }

        return false;
    }

    function findMusicButtons() {
        var allButtons = document.querySelectorAll('.emby-tab-button');
        var result = [];
        var i;
        var label;

        for (i = 0; i < allButtons.length; i += 1) {
            label = cleanText(allButtons[i]);

            if (isMusicLabel(label)) {
                result.push(allButtons[i]);
            }
        }

        return result;
    }

    function getLabels(buttons) {
        var labels = [];
        var i;

        for (i = 0; i < buttons.length; i += 1) {
            labels.push(cleanText(buttons[i]));
        }

        return labels;
    }

    function hasRequiredTabs(buttons) {
        var foundAlbums = false;
        var foundArtists = false;
        var foundSongs = false;
        var foundGenres = false;
        var i;
        var label;

        for (i = 0; i < buttons.length; i += 1) {
            label = cleanText(buttons[i]);

            if (label === 'Albums') {
                foundAlbums = true;
            }

            if (label === 'Artists') {
                foundArtists = true;
            }

            if (label === 'Songs') {
                foundSongs = true;
            }

            if (label === 'Genres') {
                foundGenres = true;
            }
        }

        if (!foundAlbums) {
            return false;
        }

        if (!foundArtists) {
            return false;
        }

        if (!foundSongs) {
            return false;
        }

        if (!foundGenres) {
            return false;
        }

        return true;
    }

    function findGenresButton(buttons) {
        var i;

        for (i = 0; i < buttons.length; i += 1) {
            if (cleanText(buttons[i]) === 'Genres') {
                return buttons[i];
            }
        }

        return null;
    }

    function getMusicLibraryId() {
        var hash = window.location.hash || '';
        var question = hash.indexOf('?');
        var query;
        var params;
        var value;

        if (question >= 0) {
            query = hash.substring(question + 1);
            params = new URLSearchParams(query);

            value = params.get('topParentId');

            if (value) {
                return value;
            }

            value = params.get('parentId');

            if (value) {
                return value;
            }
        }

        return null;
    }

    function ensureStyle() {
        var style;

        if (document.getElementById(styleId)) {
            return;
        }

        style = document.createElement('style');
        style.id = styleId;
        style.textContent =
            '#' + panelId + '{' +
                'position:fixed;' +
                'left:0;' +
                'right:0;' +
                'bottom:0;' +
                'z-index:2;' +
                'overflow:auto;' +
                'box-sizing:border-box;' +
                'padding:1.2em 0 4em;' +
                'background:var(--jf-palette-background-default,var(--theme-body-bg,#101010));' +
                'color:inherit;' +
            '}' +
            '#' + panelId + '.jf-composers-hidden{' +
                'display:none;' +
            '}' +
            '#' + panelId + ' .jf-composers-header{' +
                'display:flex;' +
                'align-items:center;' +
                'justify-content:space-between;' +
                'gap:1em;' +
                'margin:0 0 .8em;' +
            '}' +
            '#' + panelId + ' .jf-composers-header-right{' +
                'display:flex;' +
                'align-items:center;' +
                'gap:.7em;' +
                'margin-left:auto;' +
            '}' +
            '#' + panelId + ' .jf-composers-title{' +
                'margin:0;' +
            '}' +
            '#' + panelId + ' .jf-composers-count{' +
                'color:var(--jf-palette-text-secondary,rgba(255,255,255,.7));' +
                'font-size:.95em;' +
                'white-space:nowrap;' +
            '}' +
            '#' + panelId + ' .jf-composers-pager{' +
                'display:flex;' +
                'align-items:center;' +
                'gap:.1em;' +
            '}' +
            '#' + panelId + ' .jf-composers-page-button{' +
                'width:2.7em;' +
                'height:2.7em;' +
                'padding:.55em;' +
                'margin:0;' +
                'border:0;' +
                'border-radius:50%;' +
                'background:transparent;' +
                'color:var(--jf-palette-text-secondary,inherit);' +
                'cursor:pointer;' +
            '}' +
            '#' + panelId + ' .jf-composers-page-button:disabled{' +
                'opacity:.28;' +
                'cursor:default;' +
            '}' +
            '#' + panelId + ' .jf-composers-page-button .material-icons{' +
                'font-size:1.8em;' +
                'line-height:1;' +
            '}' +
            '#' + panelId + ' .jf-composers-page-label{' +
                'min-width:6.5em;' +
                'text-align:center;' +
                'color:var(--jf-palette-text-secondary,rgba(255,255,255,.7));' +
                'font-size:.95em;' +
            '}' +
            '#' + panelId + ' .jf-composers-footer{' +
                'display:flex;' +
                'align-items:center;' +
                'justify-content:flex-end;' +
                'padding-top:1.25em;' +
                'padding-bottom:1.25em;' +
            '}' +
            '#' + panelId + ' .jf-composers-footer .jf-composers-pager{' +
                'margin-left:auto;' +
            '}' +
            '#' + panelId + ' .jf-composers-status{' +
                'margin:2em 0;' +
                'font-size:1.05em;' +
                'color:var(--jf-palette-text-secondary,rgba(255,255,255,.7));' +
            '}' +
            '#' + panelId + ' .jf-composers-grid{' +
                'display:grid;' +
                'grid-template-columns:repeat(auto-fill,minmax(150px,1fr));' +
                'gap:0;' +
                'align-items:start;' +
            '}' +
            '#' + panelId + ' .jf-composer-card{' +
                'appearance:none;' +
                'border:0;' +
                'background:transparent;' +
                'color:inherit;' +
                'padding:.6em;' +
                'margin:0;' +
                'cursor:pointer;' +
                'text-align:left;' +
                'min-width:0;' +
                'font:inherit;' +
            '}' +
            '#' + panelId + ' .jf-composer-card:focus{' +
                'outline:none;' +
            '}' +
            '#' + panelId + ' .jf-composer-card:focus-visible .jf-composer-image-wrap{' +
                'outline:.25em solid var(--jf-palette-primary-main,#00a4dc);' +
                'outline-offset:.15em;' +
            '}' +
            '#' + panelId + ' .jf-composer-image-wrap{' +
                'position:relative;' +
                'aspect-ratio:1/1;' +
                'width:100%;' +
                'margin:0 0 .35em;' +
                'border-radius:.2em;' +
                'overflow:hidden;' +
                'background:#242424;' +
                'box-shadow:0 .0725em .29em 0 rgba(0,0,0,.37);' +
                'display:flex;' +
                'align-items:center;' +
                'justify-content:center;' +
                'transition:transform 200ms ease-out;' +
            '}' +
            '@media (hover:hover) and (pointer:fine){' +
                '#' + panelId + ' .jf-composer-card:hover .jf-composer-image-wrap{' +
                    'transform:scale(1.03);' +
                '}' +
            '}' +
            '#' + panelId + ' .jf-composer-image{' +
                'display:block;' +
                'width:100%;' +
                'height:100%;' +
                'object-fit:cover;' +
            '}' +
            '#' + panelId + ' .jf-composer-fallback{' +
                'font-size:2.25em;' +
                'font-weight:600;' +
                'color:var(--jf-palette-text-secondary,rgba(255,255,255,.72));' +
            '}' +
            '#' + panelId + ' .jf-composer-overview-tip{' +
                'position:absolute;' +
                'left:.45em;' +
                'right:.45em;' +
                'bottom:.45em;' +
                'z-index:2;' +
                'padding:.45em .55em;' +
                'border-radius:.25em;' +
                'background:rgba(0,0,0,.82);' +
                'color:#fff;' +
                'font-size:.82em;' +
                'line-height:1.25;' +
                'text-align:left;' +
                'white-space:normal;' +
                'opacity:0;' +
                'visibility:hidden;' +
                'transform:translateY(.2em);' +
                'transition:opacity 120ms ease-out,transform 120ms ease-out;' +
                'pointer-events:none;' +
            '}' +
            '@media (hover:hover) and (pointer:fine){' +
                '#' + panelId + ' .jf-composer-card:hover .jf-composer-overview-tip{' +
                    'opacity:1;' +
                    'visibility:visible;' +
                    'transform:translateY(0);' +
                '}' +
            '}' +
            '#' + panelId + ' .jf-composer-card:focus-visible .jf-composer-overview-tip{' +
                'opacity:1;' +
                'visibility:visible;' +
                'transform:translateY(0);' +
            '}' +
            '#' + panelId + ' .jf-composer-name{' +
                'display:block;' +
                'padding:.06em 2px;' +
                'font-size:1em;' +
                'line-height:1.25;' +
                'white-space:nowrap;' +
                'overflow:hidden;' +
                'text-overflow:ellipsis;' +
            '}' +
            '#' + tabId + '.jf-composers-selected{' +
                'font-weight:600;' +
            '}' +
            '@media (max-width:700px){' +
                '#' + panelId + ' .jf-composers-header{' +
                    'align-items:flex-start;' +
                    'flex-wrap:wrap;' +
                '}' +
                '#' + panelId + ' .jf-composers-header-right{' +
                    'width:100%;' +
                    'justify-content:space-between;' +
                    'margin-left:0;' +
                '}' +
                '#' + panelId + ' .jf-composers-grid{' +
                    'grid-template-columns:repeat(auto-fill,minmax(120px,1fr));' +
                '}' +
            '}';

        document.head.appendChild(style);
    }

    function createPager(positionName) {
        var pager;
        var previousButton;
        var previousIcon;
        var pageLabel;
        var nextButton;
        var nextIcon;

        pager = document.createElement('div');
        pager.className = 'jf-composers-pager';
        pager.setAttribute(
            'data-jf-pager-position',
            positionName
        );

        previousButton = document.createElement('button');
        previousButton.type = 'button';
        previousButton.className =
            'jf-composers-page-button paper-icon-button-light';
        previousButton.title = 'Previous 100 composers';
        previousButton.setAttribute(
            'aria-label',
            'Previous 100 composers'
        );
        previousButton.setAttribute(
            'data-jf-page-action',
            'previous'
        );

        previousIcon = document.createElement('span');
        previousIcon.className = 'material-icons chevron_left';
        previousIcon.setAttribute('aria-hidden', 'true');
        previousButton.appendChild(previousIcon);

        pageLabel = document.createElement('span');
        pageLabel.className = 'jf-composers-page-label';
        pageLabel.setAttribute('data-jf-page-label', '1');
        pageLabel.textContent = 'Page 1';

        nextButton = document.createElement('button');
        nextButton.type = 'button';
        nextButton.className =
            'jf-composers-page-button paper-icon-button-light';
        nextButton.title = 'Next 100 composers';
        nextButton.setAttribute(
            'aria-label',
            'Next 100 composers'
        );
        nextButton.setAttribute(
            'data-jf-page-action',
            'next'
        );

        nextIcon = document.createElement('span');
        nextIcon.className = 'material-icons chevron_right';
        nextIcon.setAttribute('aria-hidden', 'true');
        nextButton.appendChild(nextIcon);

        pager.appendChild(previousButton);
        pager.appendChild(pageLabel);
        pager.appendChild(nextButton);

        return pager;
    }

    function ensurePanel(tabRow) {
        var panel = document.getElementById(panelId);
        var rect;
        var header;
        var headerRight;
        var title;
        var count;
        var pager;
        var footer;
        var footerPager;
        var status;
        var grid;

        ensureStyle();

        if (!panel) {
            panel = document.createElement('div');
            panel.id = panelId;
            panel.className = 'jf-composers-hidden';

            header = document.createElement('div');
            header.className = 'jf-composers-header sectionTitleContainer sectionTitleContainer-cards padded-left padded-right';

            title = document.createElement('h2');
            title.className = 'jf-composers-title sectionTitle sectionTitle-cards';
            title.textContent = 'Composers';

            count = document.createElement('div');
            count.className = 'jf-composers-count';
            count.setAttribute('data-jf-composer-count', '1');

            pager = createPager('top');

            headerRight = document.createElement('div');
            headerRight.className = 'jf-composers-header-right';
            headerRight.appendChild(count);
            headerRight.appendChild(pager);

            header.appendChild(title);
            header.appendChild(headerRight);

            status = document.createElement('div');
            status.className = 'jf-composers-status padded-left padded-right';
            status.setAttribute('data-jf-composer-status', '1');
            status.textContent = 'Loading composers...';

            grid = document.createElement('div');
            grid.className = 'jf-composers-grid itemsContainer focuscontainer-x padded-left padded-right';
            grid.setAttribute('data-jf-composer-grid', '1');

            footer = document.createElement('div');
            footer.className =
                'jf-composers-footer padded-left padded-right';
            footerPager = createPager('bottom');
            footer.appendChild(footerPager);

            panel.appendChild(header);
            panel.appendChild(status);
            panel.appendChild(grid);
            panel.appendChild(footer);

            panel.addEventListener(
                'click',
                onPanelClick
            );

            panel.addEventListener(
                'error',
                onPanelImageError,
                true
            );

            document.body.appendChild(panel);
        }

        if (tabRow) {
            rect = tabRow.getBoundingClientRect();
            panel.style.top = Math.max(0, Math.round(rect.bottom)) + 'px';
        }

        return panel;
    }

    function setComposerSelected(selected) {
        var composerButton = document.getElementById(tabId);

        if (!composerButton) {
            return;
        }

        if (selected) {
            composerButton.classList.add('jf-composers-selected');
            composerButton.setAttribute('aria-selected', 'true');
        } else {
            composerButton.classList.remove('jf-composers-selected');
            composerButton.setAttribute('aria-selected', 'false');
        }
    }

    function showPanel() {
        var composerButton = document.getElementById(tabId);
        var tabRow = null;
        var panel;

        if (composerButton) {
            tabRow = composerButton.parentElement;
        }

        panel = ensurePanel(tabRow);
        panel.classList.remove('jf-composers-hidden');
        setComposerSelected(true);
        active = true;
    }

    function hidePanel() {
        var panel = document.getElementById(panelId);

        if (panel) {
            panel.classList.add('jf-composers-hidden');
        }

        setComposerSelected(false);
        active = false;
    }

    function showStatus(message) {
        var panel = document.getElementById(panelId);
        var status;
        var grid;
        var count;

        if (!panel) {
            return;
        }

        status = panel.querySelector('[data-jf-composer-status]');
        grid = panel.querySelector('[data-jf-composer-grid]');
        count = panel.querySelector('[data-jf-composer-count]');

        if (status) {
            status.style.display = '';
            status.textContent = message;
        }

        if (grid) {
            grid.innerHTML = '';
        }

        if (count) {
            count.textContent = '';
        }
    }

    function getInitials(name) {
        var words;
        var initials = '';
        var i;

        if (!name) {
            return '?';
        }

        words = name.split(/\s+/);

        for (i = 0; i < words.length; i += 1) {
            if (words[i]) {
                initials += words[i].charAt(0).toUpperCase();
            }

            if (initials.length >= 2) {
                break;
            }
        }

        if (!initials) {
            return '?';
        }

        return initials;
    }

    function makeImageUrl(person) {
        var url;
        var tag;

        if (!person) {
            return null;
        }

        if (!person.Id) {
            return null;
        }

        if (!person.ImageTags) {
            return null;
        }

        tag = person.ImageTags.Primary;

        if (!tag) {
            return null;
        }

        if (typeof ApiClient === 'undefined') {
            return null;
        }

        url = ApiClient.getUrl(
            'Items/' + encodeURIComponent(person.Id) + '/Images/Primary'
        );

        url += '?maxWidth=256';
        url += '&maxHeight=256';
        url += '&quality=85';
        url += '&tag=' + encodeURIComponent(tag);

        return url;
    }

    function makeOverviewPreview(overview) {
        var value;

        value = String(overview || '');
        value = value.replace(/\s+/g, ' ').trim();

        if (!value) {
            return '';
        }

        if (value.length <= 50) {
            return value;
        }

        return value.substring(0, 50).trim() + '…';
    }

    function makeComposerCard(person) {
        var card;
        var imageWrap;
        var imageUrl;
        var image;
        var fallback;
        var overviewPreview;
        var overviewTip;
        var name;

        card = document.createElement('button');
        card.type = 'button';
        card.className = 'jf-composer-card';
        card.setAttribute('data-person-id', person.Id || '');
        card.setAttribute('data-person-name', person.Name || '');
        card.setAttribute(
            'aria-label',
            'Open composer ' + (person.Name || 'Unknown')
        );

        imageWrap = document.createElement('span');
        imageWrap.className = 'jf-composer-image-wrap';

        imageUrl = makeImageUrl(person);

        if (imageUrl) {
            image = document.createElement('img');
            image.className = 'jf-composer-image';
            image.src = imageUrl;
            image.alt = '';
            image.loading = 'lazy';
            image.decoding = 'async';
            image.setAttribute('fetchpriority', 'low');
            image.setAttribute(
                'data-person-name',
                person.Name || ''
            );

            imageWrap.appendChild(image);
        } else {
            fallback = document.createElement('span');
            fallback.className = 'jf-composer-fallback';
            fallback.textContent = getInitials(person.Name || '');
            imageWrap.appendChild(fallback);
        }

        overviewPreview = makeOverviewPreview(
            person.Overview || ''
        );

        if (overviewPreview) {
            overviewTip = document.createElement('span');
            overviewTip.className = 'jf-composer-overview-tip';
            overviewTip.textContent = overviewPreview;
            imageWrap.appendChild(overviewTip);
        }

        name = document.createElement('span');
        name.className = 'jf-composer-name cardText';
        name.textContent = person.Name || 'Unknown';

        card.appendChild(imageWrap);
        card.appendChild(name);

        return card;
    }

    function comparePeople(a, b) {
        var aName = '';
        var bName = '';

        if (a && a.SortName) {
            aName = a.SortName;
        } else if (a && a.Name) {
            aName = a.Name;
        }

        if (b && b.SortName) {
            bName = b.SortName;
        } else if (b && b.Name) {
            bName = b.Name;
        }

        return aName.localeCompare(
            bName,
            undefined,
            {
                sensitivity: 'base'
            }
        );
    }

    function updatePager(itemCount, totalRecordCount) {
        var panel = document.getElementById(panelId);
        var previousButtons;
        var nextButtons;
        var pageLabels;
        var count;
        var firstItem;
        var lastItem;
        var totalPages;
        var pageText;
        var i;

        if (!panel) {
            return;
        }

        previousButtons = panel.querySelectorAll(
            '[data-jf-page-action="previous"]'
        );
        nextButtons = panel.querySelectorAll(
            '[data-jf-page-action="next"]'
        );
        pageLabels = panel.querySelectorAll(
            '[data-jf-page-label]'
        );
        count = panel.querySelector(
            '[data-jf-composer-count]'
        );

        for (i = 0; i < previousButtons.length; i += 1) {
            previousButtons[i].disabled =
                loading || currentPage <= 0;
        }

        for (i = 0; i < nextButtons.length; i += 1) {
            nextButtons[i].disabled =
                loading ||
                totalRecordCount <= 0 ||
                ((currentPage + 1) * pageSize) >= totalRecordCount;
        }

        if (totalRecordCount > 0) {
            totalPages = Math.ceil(
                totalRecordCount / pageSize
            );
            pageText =
                'Page ' + (currentPage + 1) + ' / ' + totalPages;
        } else {
            pageText = 'Page 1';
        }

        for (i = 0; i < pageLabels.length; i += 1) {
            pageLabels[i].textContent = pageText;
        }

        if (!count) {
            return;
        }

        if (itemCount <= 0 || totalRecordCount <= 0) {
            count.textContent = '0 composers';
            return;
        }

        firstItem = (currentPage * pageSize) + 1;
        lastItem = firstItem + itemCount - 1;

        count.textContent =
            firstItem + '–' + lastItem + ' of ' + totalRecordCount;
    }

    function deduplicateComposers(items) {
        var result = [];
        var seen = {};
        var i;
        var item;
        var key;

        for (i = 0; i < items.length; i += 1) {
            item = items[i];

            if (!item) {
                continue;
            }

            if (item.Id) {
                key = 'id:' + item.Id;
            } else {
                key = 'name:' + String(item.Name || '').toLowerCase();
            }

            if (!key || seen[key]) {
                continue;
            }

            seen[key] = true;
            result.push(item);
        }

        return result;
    }

    function renderComposerPage(pageNumber) {
        var panel = document.getElementById(panelId);
        var status;
        var grid;
        var fragment;
        var totalRecordCount;
        var totalPages;
        var startIndex;
        var endIndex;
        var items;
        var i;

        if (!panel) {
            return;
        }

        totalRecordCount = allComposers.length;

        if (totalRecordCount <= 0) {
            currentPage = 0;
        } else {
            totalPages = Math.ceil(totalRecordCount / pageSize);

            if (pageNumber < 0) {
                pageNumber = 0;
            }

            if (pageNumber >= totalPages) {
                pageNumber = totalPages - 1;
            }

            currentPage = pageNumber;
        }

        startIndex = currentPage * pageSize;
        endIndex = startIndex + pageSize;
        items = allComposers.slice(startIndex, endIndex);

        status = panel.querySelector('[data-jf-composer-status]');
        grid = panel.querySelector('[data-jf-composer-grid]');

        if (!grid) {
            return;
        }

        grid.innerHTML = '';

        updatePager(
            items.length,
            totalRecordCount
        );

        if (items.length === 0) {
            if (status) {
                status.style.display = '';
                status.textContent =
                    'No composers were returned for this Music library.';
            }

            updateComposerHistoryPage();
            return;
        }

        if (status) {
            status.style.display = 'none';
        }

        fragment = document.createDocumentFragment();

        for (i = 0; i < items.length; i += 1) {
            fragment.appendChild(
                makeComposerCard(items[i])
            );
        }

        grid.appendChild(fragment);
        panel.scrollTop = 0;

        updateComposerHistoryPage();

        log(
            'Rendered composer page',
            {
                page: currentPage + 1,
                composersOnPage: items.length,
                totalComposers: totalRecordCount
            }
        );
    }

    function resetComposerListForLibrary(libraryId) {
        if (composerLibraryId === libraryId) {
            return;
        }

        composerLibraryId = libraryId;
        allComposers = [];
        fullListLoaded = false;
        currentPage = 0;
        pendingPage = 0;
    }

    function loadComposers(pageNumber, isBackground) {
        var libraryId;
        var userId;
        var url;

        if (typeof pageNumber !== 'number' || pageNumber < 0) {
            pageNumber = 0;
        }

        if (typeof ApiClient === 'undefined') {
            if (!isBackground) {
                showStatus('Jellyfin ApiClient is not available.');
            }

            log('ERROR: ApiClient is not available');
            return;
        }

        libraryId = getMusicLibraryId();

        if (!libraryId) {
            if (!isBackground) {
                showStatus(
                    'Could not determine the current Music library ID from the URL.'
                );
            }

            log('ERROR: topParentId/parentId not found in Music URL');
            return;
        }

        resetComposerListForLibrary(libraryId);
        pendingPage = pageNumber;

        if (fullListLoaded) {
            if (active || !isBackground) {
                renderComposerPage(pageNumber);
            }

            return;
        }

        if (loading) {
            if (!isBackground) {
                backgroundLoad = false;
                showStatus('Loading composers...');
            }

            return;
        }

        userId = ApiClient.getCurrentUserId();
        loading = true;
        backgroundLoad = isBackground === true;

        if (!backgroundLoad) {
            showStatus('Loading composers...');
            updatePager(0, 0);
        }

        url = ApiClient.getUrl('Persons');
        url += '?PersonTypes=Composer';
        url += '&ParentId=' + encodeURIComponent(libraryId);
        url += '&Limit=10000';
        url += '&Fields=Overview';
        url += '&EnableImages=true';
        url += '&ImageTypeLimit=1';
        url += '&EnableImageTypes=Primary';

        if (userId) {
            url += '&UserId=' + encodeURIComponent(userId);
        }

        log(
            backgroundLoad ?
                'Prefetching complete Composer list' :
                'Requesting complete Composer list',
            {
                libraryId: libraryId,
                displayPage: pageNumber + 1
            }
        );

        ApiClient.getJSON(url).then(
            onComposersLoaded,
            onComposersLoadFailed
        );
    }

    function onComposersLoaded(result) {
        var sourceItems = [];

        loading = false;

        if (result && result.Items) {
            sourceItems = result.Items.slice(0);
        }

        allComposers = deduplicateComposers(sourceItems);
        allComposers.sort(comparePeople);
        fullListLoaded = true;

        log(
            'Complete Composer list loaded',
            {
                composers: allComposers.length
            }
        );

        if (active || !backgroundLoad) {
            renderComposerPage(pendingPage);
        }

        backgroundLoad = false;
    }

    function onComposersLoadFailed(error) {
        loading = false;
        fullListLoaded = false;

        if (!backgroundLoad || active) {
            updatePager(0, 0);
            showStatus(
                'Unable to load composers. Check the browser console for details.'
            );
        }

        backgroundLoad = false;
        console.error(prefix, 'Composer request failed', error);
    }

    function prefetchComposers() {
        prefetchTimer = null;

        if (active) {
            return;
        }

        if (fullListLoaded || loading) {
            return;
        }

        if (!document.getElementById(tabId)) {
            return;
        }

        loadComposers(0, true);
    }

    function scheduleComposerPrefetch() {
        if (prefetchTimer !== null) {
            return;
        }

        prefetchTimer = window.setTimeout(
            prefetchComposers,
            600
        );
    }

    function copyHistoryState() {
        var source = window.history.state;
        var target = {};
        var key;

        if (!source || typeof source !== 'object') {
            return target;
        }

        for (key in source) {
            if (Object.prototype.hasOwnProperty.call(source, key)) {
                target[key] = source[key];
            }
        }

        return target;
    }

    function isComposerHistoryState(state) {
        if (!state) {
            return false;
        }

        if (state[historyStateKey] === true) {
            return true;
        }

        return false;
    }

    function pushComposerHistoryState() {
        var currentState = window.history.state;
        var newState;

        if (isComposerHistoryState(currentState)) {
            return;
        }

        newState = copyHistoryState();
        newState[historyStateKey] = true;
        newState.jfComposersVersion = version;
        newState.jfComposersLibraryId = getMusicLibraryId();
        newState.jfComposersPage = currentPage;

        window.history.pushState(
            newState,
            document.title,
            window.location.href
        );

        log('Composer history state pushed');
    }

    function updateComposerHistoryPage() {
        var state;
        var newState;

        state = window.history.state;

        if (!isComposerHistoryState(state)) {
            return;
        }

        newState = copyHistoryState();
        newState.jfComposersPage = currentPage;
        newState.jfComposersLibraryId = getMusicLibraryId();

        window.history.replaceState(
            newState,
            document.title,
            window.location.href
        );
    }

    function restoreComposerHistoryState() {
        var hash = window.location.hash || '';
        var state = window.history.state;
        var pageNumber = 0;

        if (hash.indexOf('#/music') !== 0) {
            return;
        }

        if (
            state &&
            typeof state.jfComposersPage === 'number' &&
            state.jfComposersPage >= 0
        ) {
            pageNumber = state.jfComposersPage;
        }

        showPanel();
        loadComposers(pageNumber, false);
        log(
            'Composer view restored from history',
            {
                page: pageNumber + 1
            }
        );
    }

    function openPerson(personId) {
        var serverId;
        var route;

        if (!personId) {
            return;
        }

        if (typeof ApiClient === 'undefined') {
            return;
        }

        serverId = ApiClient.serverId();

        route = '#/details?id=' + encodeURIComponent(personId);

        if (serverId) {
            route += '&serverId=' + encodeURIComponent(serverId);
        }

        hidePanel();
        window.location.hash = route;
    }

    function onPanelClick(event) {
        var pageButton;
        var action;
        var card;

        pageButton = event.target.closest('[data-jf-page-action]');

        if (pageButton) {
            event.preventDefault();
            event.stopPropagation();
            event.stopImmediatePropagation();

            if (pageButton.disabled || loading) {
                return;
            }

            action = pageButton.getAttribute('data-jf-page-action');

            if (action === 'previous' && currentPage > 0) {
                loadComposers(currentPage - 1, false);
            } else if (
                action === 'next' &&
                ((currentPage + 1) * pageSize) < allComposers.length
            ) {
                loadComposers(currentPage + 1, false);
            }

            return;
        }

        card = event.target.closest('[data-person-id]');

        if (!card) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();

        openPerson(
            card.getAttribute('data-person-id')
        );
    }

    function onPanelImageError(event) {
        var image = event.target;
        var wrap;
        var fallback;
        var name;

        if (!image) {
            return;
        }

        if (!image.classList.contains('jf-composer-image')) {
            return;
        }

        wrap = image.parentElement;

        if (!wrap) {
            return;
        }

        name = image.getAttribute('data-person-name') || '';

        fallback = document.createElement('span');
        fallback.className = 'jf-composer-fallback';
        fallback.textContent = getInitials(name);

        wrap.replaceChild(
            fallback,
            image
        );
    }

    function onComposersClick(event) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();

        log('Composers tab clicked');

        currentPage = 0;
        pushComposerHistoryState();
        showPanel();
        loadComposers(0, false);
    }

    function onDocumentNavigationClick(event) {
        var panel;
        var composerButton;
        var target;

        if (!active) {
            return;
        }

        target = event.target;

        if (!target || typeof target.closest !== 'function') {
            return;
        }

        panel = document.getElementById(panelId);

        if (panel && panel.contains(target)) {
            return;
        }

        composerButton = document.getElementById(tabId);

        if (
            composerButton &&
            (
                target === composerButton ||
                composerButton.contains(target)
            )
        ) {
            return;
        }

        /*
         * Do not cancel or stop the native Jellyfin click. Hiding the fixed
         * Composer layer here simply gets it out of the way before Jellyfin's
         * own Home/sidebar/header/navigation handler runs.
         */
        hidePanel();
        log('Composer view closed for native Jellyfin navigation');
    }

    function onMusicTabRowClick(event) {
        var button;

        button = event.target.closest('.emby-tab-button');

        if (!button) {
            return;
        }

        if (button.id === tabId) {
            return;
        }

        if (active) {
            hidePanel();
        }
    }

    function installComposersTab() {
        var existing;
        var buttons;
        var labels;
        var labelsString;
        var genresButton;
        var composerButton;
        var tabRow;

        existing = document.getElementById(tabId);

        if (existing && existing.isConnected) {
            if (active) {
                ensurePanel(existing.parentElement);
            }

            scheduleComposerPrefetch();
            return true;
        }

        buttons = findMusicButtons();
        labels = getLabels(buttons);
        labelsString = labels.join('|');

        if (labelsString !== lastTabLabels) {
            lastTabLabels = labelsString;
            log('Detected Music buttons', labels);
        }

        if (!hasRequiredTabs(buttons)) {
            return false;
        }

        genresButton = findGenresButton(buttons);

        if (!genresButton) {
            return false;
        }

        tabRow = genresButton.parentElement;

        composerButton = document.createElement('button');
        composerButton.id = tabId;
        composerButton.type = 'button';
        composerButton.className = genresButton.className;
        composerButton.textContent = 'Composers';
        composerButton.setAttribute('role', 'tab');
        composerButton.setAttribute('aria-label', 'Composers');
        composerButton.setAttribute('aria-selected', 'false');

        composerButton.addEventListener(
            'click',
            onComposersClick,
            true
        );

        genresButton.insertAdjacentElement(
            'afterend',
            composerButton
        );

        if (tabRow) {
            if (tabRow.getAttribute('data-jf-composer-row-listener') !== '1') {
                tabRow.setAttribute(
                    'data-jf-composer-row-listener',
                    '1'
                );

                tabRow.addEventListener(
                    'click',
                    onMusicTabRowClick
                );
            }
        }

        log('SUCCESS: Composers tab inserted');
        scheduleComposerPrefetch();

        return true;
    }

    function scan() {
        installComposersTab();
    }

    function onHashChange() {
        var hash = window.location.hash || '';

        if (hash.indexOf('#/music') !== 0) {
            hidePanel();
        } else if (isComposerHistoryState(window.history.state)) {
            restoreComposerHistoryState();
        }

        lastTabLabels = '';
    }

    function onPopState(event) {
        var hash = window.location.hash || '';

        if (
            hash.indexOf('#/music') === 0 &&
            isComposerHistoryState(event.state)
        ) {
            restoreComposerHistoryState();
            return;
        }

        if (active) {
            hidePanel();
        }
    }

    function onPageShow() {
        lastTabLabels = '';
        installComposersTab();
    }

    function onWindowFocus() {
        installComposersTab();
    }

    function onVisibilityChange() {
        if (!document.hidden) {
            installComposersTab();
        }
    }

    function delayedStartupScan() {
        installComposersTab();
    }

    console.info(
        prefix,
        'STARTUP: Composer overview hover previews loaded'
    );

    window.__jfComposersV19 = {
        version: version,
        loaded: true
    };

    installComposersTab();

    scanTimer = window.setInterval(
        scan,
        750
    );

    window.setTimeout(
        delayedStartupScan,
        150
    );

    window.setTimeout(
        delayedStartupScan,
        1000
    );

    document.addEventListener(
        'click',
        onDocumentNavigationClick,
        true
    );

    window.addEventListener(
        'hashchange',
        onHashChange
    );

    window.addEventListener(
        'popstate',
        onPopState
    );

    window.addEventListener(
        'pageshow',
        onPageShow
    );

    window.addEventListener(
        'focus',
        onWindowFocus
    );

    document.addEventListener(
        'visibilitychange',
        onVisibilityChange
    );

    if (isComposerHistoryState(window.history.state)) {
        restoreComposerHistoryState();
    }

    log(
        'Composer grid, overview hover previews, top/bottom paging, navigation escape, prefetch, and recovery listeners installed'
    );
}

jfComposerMain();
