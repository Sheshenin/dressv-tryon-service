for (const form of document.querySelectorAll('.dressv-try-on-form')) {
    const submit = form.querySelector('[type=submit]');
    const cancel = form.querySelector('[data-dressv-cancel]');
    const status = form.querySelector('[data-dressv-status]');
    const image = form.querySelector('[data-dressv-result]');
    let active = null;
    let objectUrl = null;

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

    cancel.addEventListener('click', () => {
        if (active) {
            active.cancelled = true;
            active.controller.abort();
            discard(active.job);
            status.textContent = 'Примерка отменена.';
        }
    });

    window.addEventListener('pagehide', () => {
        if (active) discard(active.job);
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            image.removeAttribute('src');
            image.hidden = true;
            objectUrl = null;
        }
    });

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (active) return;
        const portrait = form.elements.portrait.files[0];
        if (!portrait) return;
        const task = {controller: new AbortController(), job: null, cancelled: false};
        active = task;
        submit.disabled = true;
        cancel.hidden = false;
        image.hidden = true;
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            image.removeAttribute('src');
            objectUrl = null;
        }
        status.textContent = 'Загружаем фото…';

        try {
            const key = crypto.randomUUID().replaceAll('-', '');
            const created = await request('create', {key, portrait});
            task.job = created.job_id;
            if (task.cancelled) return;
            const expires = Date.now() + 600_000;
            status.textContent = 'Готовим образ…';
            while (Date.now() < expires) {
                const {state} = await request('status', {job: task.job}, task.controller.signal);
                if (state === 'failed') throw new Error('Не удалось создать образ. Попробуйте ещё раз.');
                if (state === 'ready') {
                    const png = await request('result', {job: task.job}, task.controller.signal);
                    if (task.cancelled) return;
                    objectUrl = URL.createObjectURL(png);
                    image.src = objectUrl;
                    image.hidden = false;
                    status.textContent = 'Образ готов.';
                    form.elements.portrait.value = '';
                    return;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
                if (task.cancelled) return;
            }
            throw new Error('Время ожидания истекло. Попробуйте ещё раз.');
        } catch (error) {
            if (!task.cancelled) status.textContent = error.message;
        } finally {
            await discard(task.job);
            active = null;
            submit.disabled = false;
            cancel.hidden = true;
        }
    });
}
