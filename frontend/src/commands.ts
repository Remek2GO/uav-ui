import { api, ApiError, describeResult } from "./api";
import { confirmDialog } from "./Confirm";
import type { CommandResult, CommandSpec, Plugin } from "./types";

/**
 * The single way to run a plugin command. Buttons, forms, map tools and custom panels all come here,
 * so a custom panel cannot skip the confirmation.
 */
export async function runPluginCommand(
  plugin: Plugin,
  name: string,
  args: Record<string, unknown>,
): Promise<CommandResult> {
  const command = plugin.commands?.[name];
  if (!command) return { status: "rejected", error: `nie ma komendy ${name}` };

  if (command.confirm) {
    const text =
      typeof command.confirm === "string" ? command.confirm : describeCommand(command, args);
    const confirmed = await confirmDialog(command.label, text);
    if (!confirmed) return { status: "cancelled" };
  }
  try {
    return await api.command(plugin.id, name, args, Boolean(command.confirm));
  } catch (error) {
    if (error instanceof ApiError) return { status: "error", error: error.message };
    throw error;
  }
}

function describeCommand(command: CommandSpec, args: Record<string, unknown>) {
  const values = Object.entries(args)
    .map(([key, value]) => `${command.args?.[key]?.label ?? key}: ${String(value)}`)
    .join(", ");
  return values ? `Wysłać do drona? ${values}` : "Wysłać do drona?";
}

export { describeResult };
