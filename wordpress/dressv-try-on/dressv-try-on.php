<?php
/**
 * Plugin Name: Dress V Try-On
 * Description: Закрытая ИИ-примерка Dress V для карточек WooCommerce.
 * Version: 0.1.0
 * Requires PHP: 8.1
 */

defined('ABSPATH') || exit;

final class DressV_Try_On {
    private const MAX_INPUT = 16777216;
    private const MAX_RESULT = 8388608;
    private const ENDPOINT = 'https://dressv-hermes.internal:18444';
    private const MIME = ['image/png' => 'png', 'image/jpeg' => 'jpg', 'image/webp' => 'webp'];

    public static function register(): void {
        add_action('template_redirect', [self::class, 'prevent_cache']);
        add_action('woocommerce_single_product_summary', [self::class, 'render'], 32);
        add_action('wp_ajax_dressv_try_on', [self::class, 'handle']);
    }

    private static function pilot(int $product_id): array {
        $settings = get_option('dressv_pilot', []);
        if (
            !current_user_can('manage_options') || $product_id < 1
            || !is_array($settings) || (int) ($settings['product_id'] ?? 0) !== $product_id
        ) {
            return [];
        }
        return $settings;
    }

    private static function visible_product(): int {
        return function_exists('is_product') && is_product() ? get_queried_object_id() : 0;
    }

    public static function prevent_cache(): void {
        if (self::pilot(self::visible_product())) {
            if (!defined('DONOTCACHEPAGE')) {
                define('DONOTCACHEPAGE', true);
            }
            nocache_headers();
        }
    }

    public static function render(): void {
        $product_id = self::visible_product();
        if (!self::pilot($product_id)) {
            return;
        }
        wp_enqueue_script('dressv-try-on', plugins_url('dressv-try-on.js', __FILE__), [], '0.1.0', true);
        ?>
        <details class="dressv-try-on">
            <summary>Увидеть себя в образе</summary>
            <form class="dressv-try-on-form"
                  data-url="<?php echo esc_url(admin_url('admin-ajax.php', 'relative')); ?>"
                  data-product="<?php echo esc_attr((string) $product_id); ?>"
                  data-nonce="<?php echo esc_attr(wp_create_nonce('dressv-pilot-' . $product_id)); ?>">
                <p>Закрытая проверка. Загружайте только синтетические изображения.</p>
                <p><label>Фото <input name="portrait" type="file" accept="image/jpeg,image/png,image/webp" required></label></p>
                <p>
                    <button type="submit">Создать образ</button>
                    <button type="button" data-dressv-cancel hidden>Отменить</button>
                </p>
                <p role="status" aria-live="polite" data-dressv-status></p>
                <img data-dressv-result alt="Результат примерки" hidden width="320">
            </form>
        </details>
        <?php
    }

    public static function handle(): void {
        nocache_headers();
        header('Cache-Control: private, no-store');
        $product_id = isset($_POST['product']) && is_scalar($_POST['product']) ? absint($_POST['product']) : 0;
        $settings = self::pilot($product_id);
        if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST' || !$settings
            || !check_ajax_referer('dressv-pilot-' . $product_id, 'nonce', false)
        ) {
            wp_send_json_error(['message' => 'Примерка недоступна.'], 403);
        }

        $action = self::field('operation');
        $session = wp_get_session_token();
        if ($session === '') {
            wp_send_json_error(['message' => 'Обновите страницу и войдите снова.'], 403);
        }
        $owner = hash_hmac('sha256', get_current_user_id() . ':' . $product_id . ':' . $session, wp_salt('auth'));
        try {
            if ($action === 'create') {
                $key = self::field('key');
                if (!preg_match('/\A[A-Za-z0-9_-]{16,128}\z/', $key)) {
                    throw new InvalidArgumentException('Обновите страницу и повторите загрузку.');
                }
                [$boundary, $body] = self::multipart($product_id, $settings);
                [$status, $result] = self::request('POST', '/v1/jobs', $owner, $body, [
                    'Content-Type: multipart/form-data; boundary=' . $boundary,
                    'Idempotency-Key: ' . $key,
                ]);
                $job = json_decode($result, true)['job_id'] ?? '';
                if (in_array($status, [200, 202], true) && self::valid_job_id($job)) {
                    wp_send_json_success(['job_id' => $job]);
                }
                self::backend_error($status);
            }

            $job = self::field('job');
            if (!self::valid_job_id($job) || !in_array($action, ['status', 'result', 'delete'], true)) {
                throw new InvalidArgumentException('Задание недоступно.');
            }
            $path = '/v1/jobs/' . $job;
            if ($action === 'delete') {
                [$status] = self::request('DELETE', $path, $owner);
                if ($status === 204) {
                    wp_send_json_success();
                }
                self::backend_error($status);
            }
            [$status, $result] = self::request('GET', $path . ($action === 'result' ? '/result' : ''), $owner);
            if ($status !== 200) {
                self::backend_error($status);
            }
            if ($action === 'result' && str_starts_with($result, "\x89PNG\r\n\x1a\n")) {
                header('Content-Type: image/png');
                header('X-Content-Type-Options: nosniff');
                echo $result;
                wp_die('', '', ['response' => null]);
            }
            $state = json_decode($result, true)['state'] ?? '';
            if ($action === 'status' && in_array($state, ['queued', 'running', 'ready', 'failed'], true)) {
                wp_send_json_success(['state' => $state]);
            }
            self::backend_error(502);
        } catch (InvalidArgumentException $error) {
            wp_send_json_error(['message' => $error->getMessage()], 422);
        } catch (RuntimeException $error) {
            self::backend_error(502);
        }
    }

    private static function field(string $name): string {
        return isset($_POST[$name]) && is_string($_POST[$name]) ? wp_unslash($_POST[$name]) : '';
    }

    private static function valid_job_id($value): bool {
        return is_string($value) && preg_match('/\A[A-Za-z0-9_-]{32}\z/', $value) === 1;
    }

    private static function multipart(int $product_id, array $settings): array {
        $upload = $_FILES['portrait'] ?? [];
        $portrait = $upload['tmp_name'] ?? '';
        if (($upload['error'] ?? -1) !== UPLOAD_ERR_OK || !is_string($portrait) || !is_uploaded_file($portrait)) {
            throw new InvalidArgumentException('Загрузите фото в PNG, JPEG или WebP.');
        }
        $uploads = wp_get_upload_dir();
        $root = realpath($uploads['basedir']);
        $references = $settings['references'] ?? [];
        if (!$root || !is_array($references) || count($references) < 1 || count($references) > 4) {
            throw new RuntimeException('Invalid pilot references');
        }

        $paths = [['portrait', $portrait]];
        foreach ($references as $id) {
            $file = get_post_type((int) $id) === 'attachment' ? get_attached_file((int) $id) : false;
            $path = $file ? realpath($file) : false;
            if (!$path || !str_starts_with($path, $root . DIRECTORY_SEPARATOR)) {
                throw new RuntimeException('Pilot reference unavailable');
            }
            $paths[] = ['reference', $path];
        }
        $metadata = wp_json_encode([
            'schema_version' => 1,
            'product_id' => (string) $product_id,
            'prompt' => (string) ($settings['prompt'] ?? ''),
            'required_details' => array_values((array) ($settings['required_details'] ?? [])),
        ]);
        if (!$metadata || strlen($metadata) > 32000) {
            throw new RuntimeException('Invalid pilot metadata');
        }

        $boundary = 'dressv-' . bin2hex(random_bytes(24));
        $body = "--$boundary\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
              . "Content-Type: application/json\r\n\r\n$metadata\r\n";
        $remaining = self::MAX_INPUT;
        $finfo = new finfo(FILEINFO_MIME_TYPE);
        foreach ($paths as $index => [$field, $path]) {
            $data = @file_get_contents($path, false, null, 0, $remaining + 1);
            if ($data === false || $data === '' || strlen($data) > $remaining) {
                throw new InvalidArgumentException('Фото и референсы должны занимать не более 16 МиБ.');
            }
            $remaining -= strlen($data);
            $mime = $finfo->buffer($data);
            $extension = self::MIME[$mime] ?? null;
            if (!$extension) {
                throw new InvalidArgumentException('Используйте фото в PNG, JPEG или WebP.');
            }
            $body .= "--$boundary\r\nContent-Disposition: form-data; name=\"$field\"; filename=\"image-$index.$extension\"\r\n"
                   . "Content-Type: $mime\r\n\r\n$data\r\n";
        }
        return [$boundary, $body . "--$boundary--\r\n"];
    }

    private static function request(string $method, string $path, string $owner, ?string $body = null, array $headers = []): array {
        $token = @file_get_contents('/run/dressv-wordpress-token', false, null, 0, 256);
        $ca = '/run/dressv-ca.pem';
        if ($token === false || !preg_match('/\A[A-Za-z0-9_-]{16,128}\z/', trim($token))
            || !is_readable($ca) || !function_exists('curl_init')
        ) {
            throw new RuntimeException('Missing Dress V connection configuration');
        }
        $headers[] = 'Authorization: Bearer ' . trim($token);
        $headers[] = 'X-DressV-Session: ' . $owner;
        $response = '';
        $curl = curl_init(self::ENDPOINT . $path);
        curl_setopt_array($curl, [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_CAINFO => $ca,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_PROXY => '',
            CURLOPT_CONNECTTIMEOUT => 4,
            CURLOPT_TIMEOUT => 20,
            CURLOPT_WRITEFUNCTION => static function ($handle, string $chunk) use (&$response): int {
                if (strlen($response) + strlen($chunk) > self::MAX_RESULT) {
                    return 0;
                }
                $response .= $chunk;
                return strlen($chunk);
            },
        ]);
        if ($body !== null) {
            curl_setopt($curl, CURLOPT_POSTFIELDS, $body);
        }
        $ok = curl_exec($curl);
        $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        curl_close($curl);
        if ($ok === false) {
            throw new RuntimeException('Dress V connection failed');
        }
        return [$status, $response];
    }

    private static function backend_error(int $status): void {
        $messages = [
            404 => 'Задание уже завершено или истекло. Начните примерку заново.',
            409 => 'Операция сейчас недоступна. Начните примерку заново.',
            422 => 'Проверьте формат и размер фото.',
            429 => 'Примерочная занята. Попробуйте чуть позже.',
        ];
        wp_send_json_error(['message' => $messages[$status] ?? 'Примерочная временно недоступна.'], isset($messages[$status]) ? $status : 502);
    }
}

DressV_Try_On::register();
