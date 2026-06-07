import { FunctionComponent, useRef, useState } from "react";
import {
  Box,
  Group as MantineGroup,
  PasswordInput,
  Text as MantineText,
} from "@mantine/core";
import { useClipboard } from "@mantine/hooks";
import {
  faCheck,
  faClipboard,
  faSync,
} from "@fortawesome/free-solid-svg-icons";
import { range } from "lodash";
import { useSystemStatus } from "@/apis/hooks";
import {
  Action,
  Check,
  Chips,
  CollapseBox,
  File,
  Layout,
  Message,
  Number,
  Password,
  Section,
  Selector,
  Text,
} from "@/pages/Settings/components";
import { useBaseInput } from "@/pages/Settings/utilities/hooks";
import { Environment, toggleState } from "@/utilities";
import ExternalWebhookSelector from "./ExternalWebhookSelector";
import { branchOptions, proxyOptions, securityOptions } from "./options";

// Auth password input that NEVER displays the stored value.
//
// At rest, settings.auth.password holds a one-way hash (PBKDF2 since the
// post-md5 migration). Pre-populating the input with that hash would
// either reveal it via the eye toggle (useless to the user, but harmful
// in screen-shares / support bundles) or, on submit, round-trip the hash
// back to the backend. save_settings's `value != settings.auth.password`
// check would catch that exact case - but ONLY when the value sent in
// matches the stored hash byte-for-byte, which fails the moment a hash
// is encrypted-then-decrypted across a single character. So we never
// read the stored value into the input at all.
//
// State machine:
// - User loads page: input empty, draft="", touched=false. No setValue
//   call -> form has no pending change for this key -> save preserves
//   the existing hash.
// - User types "newpw": draft="newpw", touched=true, update("newpw")
//   stages the new password. save_settings hashes it via hash_password.
// - User clears the field after typing: draft="", touched=true. We push
//   the ORIGINAL hash (snapshotted on first render via originalRef)
//   back in via update(...) so save_settings's
//   `value == settings.auth.password` matches and no re-hash happens.
//
// originalRef vs reading `stored` directly: useBaseInput returns the
// staged form value, which becomes whatever update() last set it to.
// After typing "newpw", a fresh `stored` read returns "newpw", so a
// fall-back to `stored` on clear would re-stage the new password
// instead of the original hash - silently saving the typed-then-
// cancelled password. Codex flagged this. The ref captures the value
// once on first render (before any user interaction can stage anything)
// and stays pinned to the loaded hash for the lifetime of the
// component.
const AuthPasswordInput: FunctionComponent = () => {
  const { value: stored, update } = useBaseInput<
    { settingKey: string },
    string
  >({
    settingKey: "settings-auth-password",
  });
  // Capture the FIRST observed `stored` value, including the empty
  // string. Codex P3: the prior `stored.length > 0` guard meant
  // originalRef.current stayed null whenever there was no auth password
  // configured, and the type-then-clear path below sent that null back
  // through FormData, where it was serialized as the string "null" and
  // hashed by save_settings as a brand-new password.
  const originalRef = useRef<string | null>(null);
  if (originalRef.current === null && typeof stored === "string") {
    originalRef.current = stored;
  }
  const [draft, setDraft] = useState("");
  const [touched, setTouched] = useState(false);
  return (
    <PasswordInput
      label="Password"
      placeholder="Leave empty to keep current password"
      value={draft}
      onChange={(e) => {
        const next = e.currentTarget.value;
        setDraft(next);
        if (next.length > 0) {
          if (!touched) setTouched(true);
          update(next);
        } else if (touched) {
          // originalRef.current is "" when no password was configured at
          // load, or the original hash otherwise. Coalesce to "" as
          // belt-and-suspenders: NEVER hand a null to update() here, or
          // FormData turns it into the literal string "null" downstream.
          update(originalRef.current ?? "");
        }
      }}
    />
  );
};

const characters = "abcdef0123456789";
const settingApiKey = "settings-auth-apikey";

const generateApiKey = () => {
  return Array(32)
    .fill(null)
    .map(() => characters.charAt(Math.floor(Math.random() * characters.length)))
    .join("");
};

const SettingsGeneralView: FunctionComponent = () => {
  const { data: status } = useSystemStatus();
  const [copied, setCopy] = useState(false);

  const clipboard = useClipboard();

  return (
    <Layout name="General">
      <Section header="Host">
        <Text
          label="Address"
          placeholder="*"
          settingKey="settings-general-ip"
        ></Text>
        <Message>Valid IP address or '*' for all interfaces</Message>
        <Number
          label="Port"
          placeholder="6767"
          settingKey="settings-general-port"
        ></Number>
        <Text
          label="Base URL"
          leftSection="/"
          settingKey="settings-general-base_url"
          settingOptions={{
            onLoaded: (s) => s.general.base_url?.slice(1) ?? "",
            onSubmit: (v) => "/" + v,
          }}
        ></Text>
        <Message>Reverse proxy support</Message>
        <Text
          label="Instance Name"
          settingKey="settings-general-instance_name"
        ></Text>
        <Message>Have a custom instance name as browser's tab title</Message>
        <Text label="Hostname" settingKey="settings-general-hostname"></Text>
        <Message>
          Hostname or IP address to access Bazarr (ie: bazarr.mydomain.local or
          192.168.0.100). Required for webhook security.
        </Message>
      </Section>
      <Section header="Media">
        <Check
          label="Enable .strm Support"
          settingKey="settings-general-enable_strm_support"
        ></Check>
        <Message>
          Enable support for .strm files. Bazarr will read the stream URL from
          the file and analyze it for embedded tracks.
        </Message>
      </Section>
      <Section header="Security">
        <Selector
          label="Authentication"
          clearable
          options={securityOptions}
          placeholder="No Authentication"
          settingKey="settings-auth-type"
        ></Selector>
        <CollapseBox settingKey="settings-auth-type">
          <Text label="Username" settingKey="settings-auth-username"></Text>
          <AuthPasswordInput />
        </CollapseBox>
        <Text
          label="API Key"
          // User can copy through the clipboard button
          disabled={window.isSecureContext}
          // Enable user to at least copy when not in secure context
          readOnly={!window.isSecureContext}
          rightSectionWidth={95}
          rightSectionProps={{ style: { justifyContent: "flex-end" } }}
          rightSection={
            <MantineGroup gap="xs" mx="xs" justify="right">
              {
                // Clipboard API is only available in secure contexts See: https://developer.mozilla.org/en-US/docs/Web/API/Clipboard_API#interfaces
                window.isSecureContext && (
                  <Action
                    label="Copy API Key"
                    settingKey={settingApiKey}
                    c={copied ? "green" : undefined}
                    icon={copied ? faCheck : faClipboard}
                    onClick={(update, value) => {
                      if (value) {
                        clipboard.copy(value);
                        toggleState(setCopy, 1500);
                      }
                    }}
                  />
                )
              }
              <Action
                label="Regenerate"
                settingKey={settingApiKey}
                c="red"
                icon={faSync}
                onClick={(update) => {
                  update(generateApiKey());
                }}
              ></Action>
            </MantineGroup>
          }
          settingKey={settingApiKey}
        ></Text>
        <Check
          label="Enable CORS headers"
          settingKey="settings-cors-enabled"
        ></Check>
        <Message>
          Allow third parties to make requests towards your Bazarr installation.
          Requires a restart of Bazarr when changed
        </Message>
      </Section>
      <Section header="Jobs Manager">
        <Selector
          label="Concurrent Jobs"
          options={range(1, (status?.cpu_cores ?? 3) + 1).map((opt) => ({
            label: `${opt.toString()} ${opt === 1 ? "job" : "jobs"}`,
            value: opt,
          }))}
          settingKey="settings-general-concurrent_jobs"
        />
        <Message>
          Number of concurrent jobs allowed in the jobs manager.
          <br />
          This is useful to adjust the number of jobs that can be executed
          simultaneously. Exceeding jobs will be kept in pending queue until a
          slot becomes available.
          <br />
          Too much concurrent jobs can cause performance issues and affect
          system responsiveness. Setting too low can cause jobs to be queued for
          too long.
        </Message>
      </Section>
      <Section header="External Integrations">
        <ExternalWebhookSelector />
      </Section>
      <Section header="Provider Hub">
        <Check
          label="Auto-install catalog versions on startup"
          settingKey="settings-general-provider_hub_auto_install"
        ></Check>
        <Message>
          When enabled, Bazarr replaces enabled built-in providers with their
          official Provider Hub catalog versions at startup. Off by default.
          Installing or replacing providers manually from the Provider Hub
          Marketplace always works regardless of this setting.
        </Message>
      </Section>
      <Section header="Proxy">
        <Selector
          clearable
          settingKey="settings-proxy-type"
          placeholder="No Proxy"
          options={proxyOptions}
        ></Selector>
        <CollapseBox
          settingKey="settings-proxy-type"
          on={(k) => k !== null && k !== "None"}
        >
          <Text label="Host" settingKey="settings-proxy-url"></Text>
          <Number label="Port" settingKey="settings-proxy-port"></Number>
          <Text label="Username" settingKey="settings-proxy-username"></Text>
          <Password
            label="Password"
            settingKey="settings-proxy-password"
          ></Password>
          <Message>
            You only need to enter a username and password if one is required.
            Leave them blank otherwise
          </Message>
          <Chips
            label="Ignored Addresses"
            settingKey="settings-proxy-exclude"
          ></Chips>
          <Message>
            List of excluded domains or IP addresses. Asterisk(wildcard), regex
            and CIDR are unsupported. You can use '.domain.com' to include all
            subdomains.
          </Message>
        </CollapseBox>
      </Section>
      <Section header="Updates" hidden={!Environment.canUpdate}>
        <Check
          label="Automatic"
          settingKey="settings-general-auto_update"
        ></Check>
        <Message>Automatically download and install updates</Message>
        <Selector
          options={branchOptions}
          settingKey="settings-general-branch"
        ></Selector>
        <Message>Branch used by update mechanism</Message>
      </Section>
      <Section header="Logging">
        <Check label="Debug" settingKey="settings-general-debug"></Check>
        <Message>Debug logging should only be enabled temporarily</Message>
        <Text
          label="Include Filter"
          settingKey="settings-log-include_filter"
        ></Text>
        <Text
          label="Exclude Filter"
          settingKey="settings-log-exclude_filter"
        ></Text>
        <Check
          label="Use Regular Expressions (Regex)"
          settingKey="settings-log-use_regex"
        ></Check>
        <Check
          label="Ignore Case"
          settingKey="settings-log-ignore_case"
        ></Check>
      </Section>
      <Section header="Backups">
        <File
          label="Folder"
          settingKey="settings-backup-folder"
          type="bazarr"
        ></File>
        <Message>Absolute path to the backup directory</Message>
        <Number
          label="Retention"
          settingKey="settings-backup-retention"
          rightSection={
            <Box w="4rem" style={{ justifyContent: "flex-end" }}>
              <MantineText size="xs" px="sm" c="var(--bz-text-tertiary)">
                Days
              </MantineText>
            </Box>
          }
        ></Number>
      </Section>
    </Layout>
  );
};

export default SettingsGeneralView;
