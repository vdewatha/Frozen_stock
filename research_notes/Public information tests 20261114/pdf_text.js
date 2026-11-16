// PDF -> text with macOS PDFKit, for every .pdf in a folder that has no .txt beside it.
//
//     osascript -l JavaScript pdf_text.js <folder>
//
// No package to install. A scanned report has no text layer and comes out empty; the parser counts it as
// unreadable.
ObjC.import('PDFKit');
ObjC.import('Foundation');

function run(argv) {
  var folder = argv[0];
  var manager = $.NSFileManager.defaultManager;
  var names = ObjC.unwrap(manager.contentsOfDirectoryAtPathError(folder, null)) || [];
  var done = 0, failed = 0;
  for (var i = 0; i < names.length; i++) {
    var name = ObjC.unwrap(names[i]);
    if (name.slice(-4) !== '.pdf') continue;
    var target = folder + '/' + name.slice(0, -4) + '.txt';
    if (manager.fileExistsAtPath(target)) continue;
    var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(folder + '/' + name));
    var text = doc.isNil() ? null : doc.string;
    if (!text || text.isNil()) { text = $.NSString.stringWithString(''); failed++; }
    text.writeToFileAtomicallyEncodingError(target, true, $.NSUTF8StringEncoding, null);
    done++;
  }
  return 'converted=' + done + ' empty=' + failed;
}
