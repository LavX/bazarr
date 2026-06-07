import { FunctionComponent, useState } from "react";
import {
  Box,
  Button,
  Center,
  Group,
  Image,
  Stack,
  Text,
  Title,
  UnstyledButton,
} from "@mantine/core";
import { faArrowRight } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type { WhatsNewSlide } from "@/data/whatsNew";
import { useModals, withModal } from "@/modules/modals";
import { navigateApp } from "@/utilities/whatsNew";

export interface WhatsNewViewProps {
  version: string;
  slides: WhatsNewSlide[];
  /**
   * Navigation callback for slide CTAs. Defaults to the live app-navigate bridge; modals
   * render outside the Router so the modal can't use `useNavigate()` itself. Overridable
   * for testing.
   */
  onNavigate?: (to: string) => void;
}

export const WhatsNewView: FunctionComponent<WhatsNewViewProps> = ({
  version,
  slides,
  onNavigate = navigateApp,
}) => {
  const [index, setIndex] = useState(0);
  const { closeSelf } = useModals();

  if (slides.length === 0) {
    return null;
  }

  const slide = slides[index];
  const isFirst = index === 0;
  const isLast = index === slides.length - 1;
  const goTo = (next: number) =>
    setIndex(Math.max(0, Math.min(slides.length - 1, next)));

  return (
    <Stack gap="md">
      <Text size="xs" c="var(--bz-text-tertiary)">
        What&apos;s new in Bazarr+ {version}
      </Text>

      <Center mih={120}>
        {slide.image ? (
          <Image
            src={slide.image}
            alt={slide.title}
            mah={180}
            fit="contain"
            radius="md"
          />
        ) : slide.icon ? (
          <FontAwesomeIcon
            icon={slide.icon}
            size="3x"
            style={{ color: "var(--bz-text-primary)" }}
          />
        ) : null}
      </Center>

      <Title order={4}>{slide.title}</Title>
      <Text c="var(--bz-text-secondary)">{slide.body}</Text>

      {slide.cta ? (
        <Button
          variant="light"
          rightSection={<FontAwesomeIcon icon={faArrowRight} />}
          onClick={() => {
            const to = slide.cta!.to;
            closeSelf();
            // Navigate on the next tick so the router transition isn't aborted by this
            // modal subtree unmounting (otherwise the URL changes but the view doesn't).
            window.setTimeout(() => onNavigate?.(to), 0);
          }}
        >
          {slide.cta.label}
        </Button>
      ) : null}

      <Group justify="center" gap="xs" mt="xs">
        {slides.map((s, i) => (
          <UnstyledButton
            key={`${s.title}-${i}`}
            aria-label={`Go to update ${i + 1}`}
            aria-current={i === index}
            onClick={() => goTo(i)}
          >
            <Box
              w={8}
              h={8}
              style={{
                borderRadius: 9999,
                backgroundColor:
                  i === index
                    ? "var(--bz-text-primary)"
                    : "var(--bz-text-disabled)",
                transition: "background-color var(--bz-duration-fast)",
              }}
            />
          </UnstyledButton>
        ))}
      </Group>

      <Group justify="space-between" mt="xs">
        <Button
          variant="default"
          disabled={isFirst}
          onClick={() => goTo(index - 1)}
        >
          Back
        </Button>
        {isLast ? (
          <Button onClick={() => closeSelf()}>Got it</Button>
        ) : (
          <Button onClick={() => goTo(index + 1)}>Next</Button>
        )}
      </Group>
    </Stack>
  );
};

export const WhatsNewModal = withModal(WhatsNewView, "whats-new", {
  size: "md",
  title: "What's New",
});
