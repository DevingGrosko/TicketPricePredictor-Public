from pathlib import Path

path = Path("nfl_collector.py")
source = path.read_text(encoding="utf-8")

old_import = "    geometry_section_count,\n    sanitize_map_geometry,"
new_import = "    geometry_is_usable,\n    geometry_section_count,\n    sanitize_map_geometry,"
if source.count(old_import) != 1:
    raise SystemExit(
        f"Expected one geometry import block, found {source.count(old_import)}"
    )
source = source.replace(old_import, new_import)

old_block = '''                if geometry is not None:
                    captured_payload["_map_geometry"] = geometry
                    captured_payload["_map_geometry_diagnostics"] = {
                        "status": "captured",
                        "source": geometry.get("source"),
                        "mapped_sections": geometry_section_count(geometry),
                        "network_map_responses": len(map_bodies),
                    }
                    return captured_payload, event_date

                elapsed = time.monotonic() - (listings_ready_at or time.monotonic())
                if not map_view_opened and elapsed >= 0.5:
                    map_view_opened = self._open_map_view()
                if elapsed >= MAP_GEOMETRY_SETTLE_SECONDS:
                    captured_payload["_map_geometry_diagnostics"] = {
                        "status": "unavailable",
                        "network_map_responses": len(map_bodies),
                        "map_view_opened": map_view_opened,
                    }
                    return captured_payload, event_date
'''
new_block = '''                if geometry_is_usable(geometry, known_sections):
                    captured_payload["_map_geometry"] = geometry
                    captured_payload["_map_geometry_diagnostics"] = {
                        "status": "captured",
                        "source": geometry.get("source"),
                        "mapped_sections": geometry_section_count(geometry),
                        "coverage_ratio": geometry.get("coverage_ratio"),
                        "network_map_responses": len(map_bodies),
                    }
                    return captured_payload, event_date

                elapsed = time.monotonic() - (listings_ready_at or time.monotonic())
                if not map_view_opened and elapsed >= 0.5:
                    map_view_opened = self._open_map_view()
                if elapsed >= MAP_GEOMETRY_SETTLE_SECONDS:
                    if geometry is not None:
                        captured_payload["_map_geometry"] = geometry
                    captured_payload["_map_geometry_diagnostics"] = {
                        "status": "partial" if geometry is not None else "unavailable",
                        "source": geometry.get("source") if geometry else None,
                        "mapped_sections": geometry_section_count(geometry),
                        "coverage_ratio": geometry.get("coverage_ratio") if geometry else 0,
                        "network_map_responses": len(map_bodies),
                        "map_view_opened": map_view_opened,
                    }
                    return captured_payload, event_date
'''
if source.count(old_block) != 1:
    raise SystemExit(
        f"Expected one capture geometry block, found {source.count(old_block)}"
    )
path.write_text(source.replace(old_block, new_block), encoding="utf-8")
