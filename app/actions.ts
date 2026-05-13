"use server";

import { revalidatePath } from "next/cache";
import { deleteSource, markLaterhubFinished, saveSource, toggleSource } from "@/lib/data";

export async function saveSourceAction(formData: FormData) {
  saveSource({
    source_id: String(formData.get("source_id") || ""),
    name: String(formData.get("name") || ""),
    site_url: String(formData.get("site_url") || ""),
    feed_url: String(formData.get("feed_url") || "")
  });
  revalidatePath("/");
}

export async function toggleSourceAction(formData: FormData) {
  toggleSource(String(formData.get("source_id") || ""), String(formData.get("enabled") || "0") === "1");
  revalidatePath("/");
}

export async function deleteSourceAction(formData: FormData) {
  deleteSource(String(formData.get("source_id") || ""));
  revalidatePath("/");
}

export async function finishLaterhubAction(formData: FormData) {
  markLaterhubFinished(Number(formData.get("id") || 0), String(formData.get("finished") || "0") === "1");
  revalidatePath("/");
}
