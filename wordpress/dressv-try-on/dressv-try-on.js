for (const form of document.querySelectorAll('.dressv-try-on-form')) {
    const wrapper = form.closest('.dressv-try-on');
    const dialog = wrapper.querySelector('.dressv-dialog');
    const open = wrapper.querySelector('[data-dressv-open]');
    const wait = form.querySelector('[data-dressv-wait]');
    const time = form.querySelector('[data-dressv-time]');
    open.addEventListener('click', () => dialog.showModal());
    wrapper.querySelector('[data-dressv-close]').addEventListener('click', () => dialog.close());

    const submit = form.querySelector('[type=submit]');
    const cancel = form.querySelector('[data-dressv-cancel]');
    const status = form.querySelector('[data-dressv-status]');
    const image = form.querySelector('[data-dressv-result]');
    const zoom = form.querySelector('[data-dressv-zoom]');
    const fullscreen = wrapper.querySelector('.dressv-fullscreen');
    const fullImage = fullscreen.querySelector('[data-dressv-full-image]');
    zoom.addEventListener('click', () => {
        if (!objectUrl || fullscreen.open) return;
        fullImage.src = objectUrl;
        fullscreen.showModal();
    });
    fullscreen.querySelector('[data-dressv-full-close]').addEventListener('click', () => fullscreen.close());
    fullscreen.addEventListener('close', () => fullImage.removeAttribute('src'));

    const preview = form.querySelector('[data-dressv-preview]');
    const input = form.elements.portrait;
    let active = null;
    let objectUrl = null;
    let previewUrl = null;

    input.addEventListener('change', () => {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        const file = input.files[0];
        previewUrl = file ? URL.createObjectURL(file) : null;
        if (previewUrl) {
            preview.src = previewUrl;
        } else {
            preview.removeAttribute('src');
        }
        preview.hidden = !previewUrl;
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            image.removeAttribute('src');
            zoom.hidden = true;
            objectUrl = null;
        }
        status.textContent = '';
    });

    const request = async (operation, values = {}, signal, keepalive = false) => {
        const body = new FormData();
        for (const [key, value] of Object.entries({
            action: 'dressv_try_on', product: form.dataset.product,
            nonce: form.dataset.nonce, operation, ...values,
        })) {
            body.set(key, value);
        }
        const response = await fetch(form.dataset.url, {
            method: 'POST', body, signal, keepalive, cache: 'no-store', credentials: 'same-origin',
        });
        if (operation === 'result' && response.ok && response.headers.get('Content-Type')?.startsWith('image/png')) {
            return response.blob();
        }
        const json = await response.json().catch(() => ({}));
        if (!response.ok || !json.success) {
            throw new Error(json.data?.message || 'Примерочная временно недоступна.');
        }
        return json.data;
    };

    const discard = job => job ? request('delete', {job}, undefined, true).catch(() => {}) : Promise.resolve();

    const cancelJob = () => {
        if (active) {
            active.cancelled = true;
            active.controller.abort();
            status.textContent = 'Примерка отменена.';
        }
    };
    cancel.addEventListener('click', cancelJob);
    dialog.addEventListener('close', cancelJob);

    window.addEventListener('pagehide', () => {
        if (fullscreen.open) fullscreen.close();
        if (active) discard(active.job);
        if (previewUrl) {
            URL.revokeObjectURL(previewUrl);
            preview.removeAttribute('src');
            preview.hidden = true;
            previewUrl = null;
        }
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            image.removeAttribute('src');
            zoom.hidden = true;
            objectUrl = null;
        }
    });

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (active) return;
        const portrait = input.files[0];
        if (!portrait) return;
        const task = {controller: new AbortController(), job: null, cancelled: false};
        active = task;
        submit.disabled = true;
        input.disabled = true;
        cancel.hidden = false;
        zoom.hidden = true;
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            image.removeAttribute('src');
            objectUrl = null;
        }
        status.textContent = 'Загружаем фото…';

        let ticker;
        try {
            const key = crypto.randomUUID().replaceAll('-', '');
            const created = await request('create', {key, portrait});
            task.job = created.job_id;
            if (task.cancelled) return;
            const expires = Date.now() + 600_000;
            status.textContent = 'Готовим образ. Дайте генерации примерно минуту.';
            wait.hidden = false;
            const started = Date.now();
            const updateTime = () => {
                const seconds = Math.floor((Date.now() - started) / 1000);
                time.textContent = seconds < 15
                    ? `Первые секунды · ${15 - seconds} с`
                    : `Генерация продолжается · прошло ${seconds} с`;
            };
            updateTime();
            ticker = setInterval(updateTime, 250);
            while (Date.now() < expires) {
                const {state} = await request('status', {job: task.job}, task.controller.signal);
                if (state === 'failed') throw new Error('Не удалось создать образ. Попробуйте ещё раз.');
                if (state === 'ready') {
                    const png = await request('result', {job: task.job}, task.controller.signal);
                    if (task.cancelled) return;
                    objectUrl = URL.createObjectURL(png);
                    image.src = objectUrl;
                    zoom.hidden = false;
                    status.textContent = 'Образ готов.';
                    return;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
                if (task.cancelled) return;
            }
            throw new Error('Время ожидания истекло. Попробуйте ещё раз.');
        } catch (error) {
            if (!task.cancelled) status.textContent = error.message;
        } finally {
            clearInterval(ticker);
            wait.hidden = true;
            await discard(task.job);
            active = null;
            submit.disabled = false;
            input.disabled = false;
            cancel.hidden = true;
        }
    });
}
