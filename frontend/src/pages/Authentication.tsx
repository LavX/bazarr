import { FunctionComponent, useEffect, useRef, useState } from "react";
import { Button, PasswordInput, Stack, Text, TextInput } from "@mantine/core";
import { useForm } from "@mantine/form";
import { useSystem } from "@/apis/hooks";
import { Environment } from "@/utilities";
import styles from "./Authentication.module.scss";

interface BackdropsResponse {
  backdrops: string[];
}

// How long each backdrop stays before cross-fading to the next.
const ROTATE_INTERVAL_MS = 9000;

const prefersReducedMotion = (): boolean =>
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const Authentication: FunctionComponent = () => {
  const { login } = useSystem();

  const [backdrops, setBackdrops] = useState<string[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const rotateRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const form = useForm({
    initialValues: {
      username: "",
      password: "",
    },
  });

  // Fetch the (unauthenticated) backdrop list on mount, then preload each
  // image so the cross-fade has bytes ready. Any failure leaves the plain
  // Atmospheric Dark gradient in place.
  useEffect(() => {
    let cancelled = false;

    const controller = new AbortController();
    fetch(`${Environment.baseUrl}/system/backdrops`, {
      signal: controller.signal,
    })
      .then((res) => (res.ok ? res.json() : { backdrops: [] }))
      .then((data: BackdropsResponse) => {
        if (cancelled || !Array.isArray(data.backdrops)) {
          return;
        }
        const urls = data.backdrops;
        // Preload so the first paint and subsequent fades are smooth.
        urls.forEach((url) => {
          const img = new Image();
          img.src = url;
        });
        setBackdrops(urls);
      })
      .catch(() => {
        // Network/abort error: keep the gradient fallback.
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  // Auto-rotate backdrops unless the user prefers reduced motion.
  useEffect(() => {
    if (backdrops.length <= 1 || prefersReducedMotion()) {
      return;
    }
    rotateRef.current = setInterval(() => {
      setActiveIndex((prev) => (prev + 1) % backdrops.length);
    }, ROTATE_INTERVAL_MS);

    return () => {
      if (rotateRef.current !== null) {
        clearInterval(rotateRef.current);
      }
    };
  }, [backdrops]);

  return (
    <div className={styles.page}>
      <div className={styles.backdrops} aria-hidden="true">
        {backdrops.map((url, index) => (
          <div
            key={url}
            data-testid="login-backdrop"
            className={`${styles.backdrop} ${
              index === activeIndex ? styles.backdropActive : ""
            }`}
            style={{ backgroundImage: `url("${url}")` }}
          />
        ))}
      </div>
      <div className={styles.overlay} aria-hidden="true" />
      <div className={styles.glow} aria-hidden="true" />

      <section className={styles.card}>
        <Stack>
          <div className={styles.brand}>
            <img
              className={styles.logo}
              src={`${Environment.baseUrl}/images/logo_no_orb128.png`}
              alt="Bazarr+"
            />
            <Text className={styles.wordmark} component="div">
              Bazarr<span className={styles.accent}>+</span>
            </Text>
            <Text className={styles.subtitle} component="div">
              Sign in to your library
            </Text>
          </div>
          <form
            onSubmit={form.onSubmit((values) => {
              login(values);
            })}
          >
            <Stack>
              <TextInput
                name="Username"
                label="Username"
                placeholder="Username"
                required
                {...form.getInputProps("username")}
              ></TextInput>
              <PasswordInput
                name="Password"
                label="Password"
                required
                placeholder="Password"
                {...form.getInputProps("password")}
              ></PasswordInput>
              <Button
                className={styles.submit}
                fullWidth
                mt="sm"
                tt="uppercase"
                type="submit"
              >
                Login
              </Button>
            </Stack>
          </form>
        </Stack>
      </section>
    </div>
  );
};

export default Authentication;
